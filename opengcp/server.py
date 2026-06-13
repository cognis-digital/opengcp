"""Single local HTTP server exposing all opengcp services under path prefixes.

Path layout (all JSON unless noted):

  Storage   (GCS-style)
    POST   /storage/b/<bucket>                          create bucket (?versioning=1)
    GET    /storage/b                                   list buckets
    GET    /storage/b/<bucket>                          get bucket
    DELETE /storage/b/<bucket>                          delete bucket
    PATCH  /storage/b/<bucket>?versioning=1|0           enable/disable versioning
    PATCH  /storage/b/<bucket>/lifecycle                set lifecycle rules (json body)
    POST   /storage/b/<bucket>/o/<name...>              upload object (raw body)
    GET    /storage/b/<bucket>/o/<name...>              download object (raw body)
    GET    /storage/b/<bucket>/o/<name...>?meta=1       object metadata (json)
    PATCH  /storage/b/<bucket>/o/<name...>              update custom metadata (json)
    GET    /storage/b/<bucket>/o                        list objects (?prefix=&delimiter=&versions=1)
    DELETE /storage/b/<bucket>/o/<name...>              delete object (?generation=N)
    POST   /storage/b/<bucket>/o/<name...>/copy?dst=<b/n> server-side copy
    POST   /storage/b/<bucket>/o/<name...>/compose     compose (json {sources:[],contentType})

  Firestore (document) style
    POST   /firestore/<collection>                   create doc (json body)
    GET    /firestore/<collection>                   list / query (?field&op&value)
    GET    /firestore/<collection>/<id>              get doc
    PUT    /firestore/<collection>/<id>              set doc
    PATCH  /firestore/<collection>/<id>              update doc
    DELETE /firestore/<collection>/<id>              delete doc

  Pub/Sub style
    POST   /pubsub/topics/<topic>                    create topic
    GET    /pubsub/topics                            list topics
    DELETE /pubsub/topics/<topic>                    delete topic
    POST   /pubsub/topics/<topic>/publish            publish (json body)
    POST   /pubsub/subscriptions/<name>?topic=...    create subscription
    GET    /pubsub/subscriptions                     list subscriptions
    POST   /pubsub/subscriptions/<name>/pull         pull (?max=)
    POST   /pubsub/subscriptions/<name>/ack          ack (json {ackIds:[]})

  Functions style
    GET    /functions                                list registered functions
    GET    /functions/invocations                    invocation log

  Datastore style
    GET    /datastore/kinds                          list kinds
    POST   /datastore/<kind>                         put entity (json body; ?id=)
    GET    /datastore/<kind>                         list kind / GQL query (?gql=)
    GET    /datastore/<kind>/<id>                    get entity
    PUT    /datastore/<kind>/<id>                    upsert entity
    DELETE /datastore/<kind>/<id>                    delete entity

  Bigtable style
    POST   /bigtable/instances/<inst>                create instance
    GET    /bigtable/instances                       list instances
    DELETE /bigtable/instances/<inst>                delete instance
    POST   /bigtable/instances/<inst>/tables/<tbl>   create table
    GET    /bigtable/instances/<inst>/tables         list tables
    DELETE /bigtable/instances/<inst>/tables/<tbl>   delete table
    POST   /bigtable/instances/<inst>/tables/<tbl>/families/<fam>  create column family (?maxVersions=)
    GET    /bigtable/instances/<inst>/tables/<tbl>/families         list column families
    DELETE /bigtable/instances/<inst>/tables/<tbl>/families/<fam>  delete column family
    POST   /bigtable/instances/<inst>/tables/<tbl>/rows/<key>/mutate  mutate row (json body)
    GET    /bigtable/instances/<inst>/tables/<tbl>/rows/<key>         read row (?families=)
    GET    /bigtable/instances/<inst>/tables/<tbl>/rows               scan rows (?start=&end=&prefix=&limit=&families=)

  BigQuery style
    POST   /bigquery/datasets/<ds>                   create dataset
    GET    /bigquery/datasets                        list datasets
    GET    /bigquery/datasets/<ds>                   get dataset
    DELETE /bigquery/datasets/<ds>                   delete dataset (?deleteContents=1)
    POST   /bigquery/datasets/<ds>/tables/<tbl>      create table (json body: {schema})
    GET    /bigquery/datasets/<ds>/tables            list tables
    GET    /bigquery/datasets/<ds>/tables/<tbl>      get table metadata
    DELETE /bigquery/datasets/<ds>/tables/<tbl>      delete table
    POST   /bigquery/datasets/<ds>/tables/<tbl>/insertAll  streaming insert (json body)
    POST   /bigquery/query                           run query (json body: {query})

  Misc
    GET    /healthz                                  liveness probe
"""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

from . import storage as storage_mod
from . import firestore as firestore_mod
from . import pubsub as pubsub_mod
from . import datastore as datastore_mod
from . import bigtable as bigtable_mod
from . import bigquery as bigquery_mod
from .storage import ObjectStorage
from .firestore import DocumentStore
from .pubsub import PubSub
from .functions import FunctionRunner
from .datastore import DatastoreDB, Key as DSKey
from .bigtable import BigtableAdmin, SetCell, DeleteCell, DeleteFromFamily, DeleteFromRow
from .bigquery import BigQueryDB


class Services:
    """Container wiring all services together."""

    def __init__(self, data_dir=None):
        if data_dir:
            import os
            os.makedirs(data_dir, exist_ok=True)
            self.storage = ObjectStorage(root=os.path.join(data_dir, "storage"))
            self.firestore = DocumentStore(path=os.path.join(data_dir, "firestore.db"))
            self.datastore = DatastoreDB(path=os.path.join(data_dir, "datastore.db"))
            self.bigquery = BigQueryDB(path=os.path.join(data_dir, "bigquery.db"))
        else:
            self.storage = ObjectStorage(root=None)
            self.firestore = DocumentStore(path=None)
            self.datastore = DatastoreDB(path=None)
            self.bigquery = BigQueryDB(path=None)
        self.pubsub = PubSub()
        self.functions = FunctionRunner(storage=self.storage, pubsub=self.pubsub)
        self.bigtable = BigtableAdmin()


def _make_handler(services: Services):

    class Handler(BaseHTTPRequestHandler):
        server_version = "opengcp/0.1"
        protocol_version = "HTTP/1.1"

        # silence default logging during tests
        def log_message(self, fmt, *args):  # noqa: A003
            pass

        # ----- helpers -----
        def _send_json(self, code, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_raw(self, code, body, content_type="application/octet-stream"):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _error(self, code, msg):
            self._send_json(code, {"error": msg})

        def _read_body(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            return self.rfile.read(length) if length else b""

        def _json_body(self):
            raw = self._read_body()
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))

        def _path_parts(self):
            parsed = urlparse(self.path)
            parts = [unquote(p) for p in parsed.path.split("/") if p != ""]
            query = parse_qs(parsed.query)
            return parts, query

        # ----- method routing -----
        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_PUT(self):
            self._route("PUT")

        def do_PATCH(self):
            self._route("PATCH")

        def do_DELETE(self):
            self._route("DELETE")

        def _route(self, method):
            parts, query = self._path_parts()
            try:
                if not parts:
                    return self._send_json(200, {"service": "opengcp",
                                                 "endpoints": ["/storage", "/firestore",
                                                               "/pubsub", "/functions",
                                                               "/datastore", "/bigtable",
                                                               "/bigquery", "/healthz"]})
                head = parts[0]
                if head == "healthz":
                    return self._send_json(200, {"status": "ok"})
                if head == "storage":
                    return self._route_storage(method, parts[1:], query)
                if head == "firestore":
                    return self._route_firestore(method, parts[1:], query)
                if head == "pubsub":
                    return self._route_pubsub(method, parts[1:], query)
                if head == "functions":
                    return self._route_functions(method, parts[1:], query)
                if head == "datastore":
                    return self._route_datastore(method, parts[1:], query)
                if head == "bigtable":
                    return self._route_bigtable(method, parts[1:], query)
                if head == "bigquery":
                    return self._route_bigquery(method, parts[1:], query)
                return self._error(404, f"unknown service: {head}")
            except (storage_mod.StorageError, firestore_mod.FirestoreError,
                    pubsub_mod.PubSubError,
                    datastore_mod.DatastoreError,
                    bigtable_mod.BigtableError,
                    bigquery_mod.BigQueryError) as exc:
                code = 404 if "NotFound" in type(exc).__name__ else 409
                return self._error(code, f"{type(exc).__name__}: {exc}")
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                return self._error(400, f"bad request: {exc}")
            except BrokenPipeError:
                pass
            except Exception as exc:  # noqa: BLE001
                return self._error(500, f"internal error: {exc}")

        # ----- storage -----
        def _route_storage(self, method, p, query):
            s = services.storage
            # /storage/b ...
            if not p or p[0] != "b":
                return self._error(404, "expected /storage/b/...")
            p = p[1:]
            if not p:
                if method == "GET":
                    return self._send_json(200, {"items": s.list_buckets()})
                return self._error(405, method)
            bucket = p[0]
            rest = p[1:]
            if not rest:
                if method == "POST":
                    ver = query.get("versioning", ["0"])[0] in ("1", "true")
                    return self._send_json(200, s.create_bucket(bucket,
                                                                versioning_enabled=ver))
                if method == "GET":
                    return self._send_json(200, s.get_bucket(bucket))
                if method == "DELETE":
                    s.delete_bucket(bucket)
                    return self._send_json(200, {"deleted": bucket})
                if method == "PATCH":
                    # PATCH /storage/b/<bucket>?versioning=1|0
                    if "versioning" in query:
                        ver = query["versioning"][0] in ("1", "true")
                        return self._send_json(200, s.set_versioning(bucket, ver))
                    return self._error(400, "unsupported bucket PATCH")
                return self._error(405, method)

            # /storage/b/<bucket>/lifecycle
            if rest[0] == "lifecycle":
                if method == "PATCH" and len(rest) == 1:
                    body = self._json_body()
                    rules = body.get("rules", body) if isinstance(body, dict) else body
                    if not isinstance(rules, list):
                        rules = [rules]
                    return self._send_json(200, s.set_lifecycle(bucket, rules))
                return self._error(405, method)

            if rest[0] != "o":
                return self._error(404, "expected /storage/b/<bucket>/o/...")
            obj_parts = rest[1:]
            if not obj_parts:
                if method == "GET":
                    prefix = query.get("prefix", [""])[0]
                    delimiter = query.get("delimiter", [""])[0]
                    show_versions = query.get("versions", ["0"])[0] in ("1", "true")
                    items, prefixes = s.list_objects(bucket, prefix,
                                                     delimiter=delimiter,
                                                     versions=show_versions)
                    return self._send_json(200, {
                        "items": [m.to_dict() for m in items],
                        "prefixes": prefixes,
                    })
                return self._error(405, method)
            name = "/".join(obj_parts)

            # sub-actions: copy, compose
            if len(rest) >= 3 and rest[-1] == "copy":
                name = "/".join(obj_parts[:-1])
                if method == "POST":
                    dst = query.get("dst", [None])[0]
                    if not dst or "/" not in dst:
                        return self._error(400, "missing or invalid ?dst=<bucket>/<name>")
                    dst_bucket, _, dst_name = dst.partition("/")
                    meta = s.copy_object(bucket, name, dst_bucket, dst_name)
                    return self._send_json(200, meta.to_dict())
                return self._error(405, method)

            if len(rest) >= 3 and rest[-1] == "compose":
                name = "/".join(obj_parts[:-1])
                if method == "POST":
                    body = self._json_body()
                    sources = body.get("sources", [])
                    ct = body.get("contentType", "application/octet-stream")
                    meta = s.compose(bucket, name, sources, content_type=ct)
                    return self._send_json(200, meta.to_dict())
                return self._error(405, method)

            if method == "POST":
                data = self._read_body()
                ct = self.headers.get("Content-Type", "application/octet-stream")
                # custom metadata passed as X-Goog-Meta-* headers
                _prefix = "x-goog-meta-"
                user_meta = {}
                for hdr, val in self.headers.items():
                    hdr_lower = hdr.lower()
                    if hdr_lower.startswith(_prefix):
                        user_meta[hdr_lower[len(_prefix):]] = val
                meta = s.upload(bucket, name, data, content_type=ct,
                                metadata=user_meta or None)
                services.functions.fire_object_finalize(bucket, name, size=meta.size)
                return self._send_json(200, meta.to_dict())
            if method == "GET":
                gen_str = query.get("generation", [None])[0]
                gen = int(gen_str) if gen_str else None
                if query.get("meta", ["0"])[0] in ("1", "true"):
                    return self._send_json(200, s.stat(bucket, name,
                                                       generation=gen).to_dict())
                data = s.download(bucket, name, generation=gen)
                ct = s.stat(bucket, name, generation=gen).content_type
                return self._send_raw(200, data, content_type=ct)
            if method == "PATCH":
                body = self._json_body()
                md = body.get("metadata", body)
                meta = s.update_metadata(bucket, name, md)
                return self._send_json(200, meta.to_dict())
            if method == "DELETE":
                gen_str = query.get("generation", [None])[0]
                if gen_str:
                    s.delete_version(bucket, name, int(gen_str))
                else:
                    s.delete(bucket, name)
                return self._send_json(200, {"deleted": name})
            return self._error(405, method)

        # ----- firestore -----
        def _route_firestore(self, method, p, query):
            db = services.firestore
            if not p:
                if method == "GET":
                    return self._send_json(200, {"collections": db.collections()})
                return self._error(405, method)
            collection = p[0]
            if len(p) == 1:
                if method == "POST":
                    data = self._json_body()
                    doc_id = db.create(collection, data)
                    return self._send_json(200, {"id": doc_id, "data": data})
                if method == "GET":
                    if "field" in query:
                        field = query["field"][0]
                        op = query.get("op", ["=="])[0]
                        raw = query.get("value", [""])[0]
                        try:
                            value = json.loads(raw)
                        except json.JSONDecodeError:
                            value = raw
                        lim = query.get("limit", [None])[0]
                        lim = int(lim) if lim else None
                        rows = db.query(collection, field, op, value, limit=lim)
                    else:
                        rows = db.list(collection)
                    return self._send_json(200, {"documents": [
                        {"id": i, "data": d} for i, d in rows]})
                return self._error(405, method)
            doc_id = p[1]
            if method == "GET":
                return self._send_json(200, {"id": doc_id,
                                             "data": db.get(collection, doc_id)})
            if method == "PUT":
                data = self._json_body()
                db.set(collection, doc_id, data)
                return self._send_json(200, {"id": doc_id, "data": data})
            if method == "PATCH":
                fields = self._json_body()
                doc = db.update(collection, doc_id, fields)
                return self._send_json(200, {"id": doc_id, "data": doc})
            if method == "DELETE":
                db.delete(collection, doc_id)
                return self._send_json(200, {"deleted": doc_id})
            return self._error(405, method)

        # ----- pubsub -----
        def _route_pubsub(self, method, p, query):
            ps = services.pubsub
            if not p:
                return self._error(404, "expected /pubsub/...")
            kind = p[0]
            rest = p[1:]
            if kind == "topics":
                if not rest:
                    if method == "GET":
                        return self._send_json(200, {"topics": ps.list_topics()})
                    return self._error(405, method)
                topic = rest[0]
                if len(rest) == 1:
                    if method == "POST":
                        ps.create_topic(topic)
                        return self._send_json(200, {"topic": topic})
                    if method == "DELETE":
                        ps.delete_topic(topic)
                        return self._send_json(200, {"deleted": topic})
                    return self._error(405, method)
                if rest[1] == "publish" and method == "POST":
                    body = self._json_body()
                    data = body.get("data", "")
                    if body.get("dataEncoding") == "base64":
                        data = base64.b64decode(data)
                    attrs = body.get("attributes", {})
                    mid = ps.publish(topic, data, attributes=attrs)
                    return self._send_json(200, {"messageId": mid})
                return self._error(404, "bad topic route")
            if kind == "subscriptions":
                if not rest:
                    if method == "GET":
                        return self._send_json(200,
                                               {"subscriptions": ps.list_subscriptions()})
                    return self._error(405, method)
                name = rest[0]
                if len(rest) == 1:
                    if method == "POST":
                        topic = query.get("topic", [None])[0]
                        if not topic:
                            return self._error(400, "missing ?topic=")
                        ad = float(query.get("ackDeadline", ["10"])[0])
                        ps.create_subscription(name, topic, ack_deadline=ad)
                        return self._send_json(200, {"subscription": name,
                                                     "topic": topic})
                    if method == "DELETE":
                        ps.delete_subscription(name)
                        return self._send_json(200, {"deleted": name})
                    if method == "GET":
                        return self._send_json(200, ps.stats(name))
                    return self._error(405, method)
                action = rest[1]
                if action == "pull" and method == "POST":
                    mx = int(query.get("max", ["10"])[0])
                    return self._send_json(200,
                                           {"receivedMessages": ps.pull(name, mx)})
                if action == "ack" and method == "POST":
                    body = self._json_body()
                    n = ps.ack(name, body.get("ackIds", []))
                    return self._send_json(200, {"acked": n})
                if action == "nack" and method == "POST":
                    body = self._json_body()
                    n = ps.nack(name, body.get("ackIds", []))
                    return self._send_json(200, {"nacked": n})
                return self._error(404, "bad subscription route")
            return self._error(404, f"unknown pubsub kind: {kind}")

        # ----- functions -----
        def _route_functions(self, method, p, query):
            fr = services.functions
            if not p:
                if method == "GET":
                    return self._send_json(200, {"functions": fr.list_functions()})
                return self._error(405, method)
            if p[0] == "invocations" and method == "GET":
                fn = query.get("function", [None])[0]
                invs = fr.invocations(fn)
                return self._send_json(200, {"invocations": [
                    {"function": i.function, "eventType": i.event_type,
                     "resource": i.resource, "ok": i.ok, "result": i.result,
                     "error": i.error, "timestamp": i.timestamp} for i in invs]})
            return self._error(404, "bad functions route")

        # ----- datastore -----
        def _route_datastore(self, method, p, query):
            ds = services.datastore
            if not p:
                return self._error(404, "expected /datastore/...")
            if p[0] == "kinds":
                if method == "GET":
                    return self._send_json(200, {"kinds": ds.kinds()})
                return self._error(405, method)
            kind = p[0]
            if len(p) == 1:
                if method == "POST":
                    body = self._json_body()
                    id_raw = query.get("id", [None])[0]
                    if id_raw is not None:
                        try:
                            eid = int(id_raw)
                        except ValueError:
                            eid = id_raw
                    else:
                        eid = None
                    key = ds.put(DSKey(kind, eid), body)
                    return self._send_json(200, {"key": key.to_dict(), "data": body})
                if method == "GET":
                    gql_str = query.get("gql", [None])[0]
                    if gql_str:
                        results = ds.gql(gql_str)
                    else:
                        results = ds.list_kind(kind)
                    return self._send_json(200, {"entities": [
                        {"key": k.to_dict(), "data": d} for k, d in results]})
                return self._error(405, method)
            raw_id = p[1]
            try:
                eid = int(raw_id)
            except ValueError:
                eid = raw_id
            key = DSKey(kind, eid)
            if method == "GET":
                data = ds.get(key)
                return self._send_json(200, {"key": key.to_dict(), "data": data})
            if method == "PUT":
                body = self._json_body()
                ds.put(key, body)
                return self._send_json(200, {"key": key.to_dict(), "data": body})
            if method == "DELETE":
                ds.delete(key)
                return self._send_json(200, {"deleted": key.to_dict()})
            return self._error(405, method)

        # ----- bigtable -----
        def _route_bigtable(self, method, p, query):
            bt = services.bigtable
            if not p or p[0] != "instances":
                return self._error(404, "expected /bigtable/instances/...")
            p = p[1:]
            if not p:
                if method == "GET":
                    return self._send_json(200, {"instances": bt.list_instances()})
                return self._error(405, method)
            inst_id = p[0]
            rest = p[1:]
            if not rest:
                if method == "POST":
                    inst = bt.create_instance(inst_id)
                    return self._send_json(200, inst.to_dict())
                if method == "GET":
                    return self._send_json(200, bt.get_instance(inst_id).to_dict())
                if method == "DELETE":
                    bt.delete_instance(inst_id)
                    return self._send_json(200, {"deleted": inst_id})
                return self._error(405, method)
            if rest[0] != "tables":
                return self._error(404, "expected /bigtable/instances/<i>/tables/...")
            rest = rest[1:]
            inst = bt.get_instance(inst_id)
            if not rest:
                if method == "GET":
                    return self._send_json(200, {"tables": inst.list_tables()})
                return self._error(405, method)
            tbl_id = rest[0]
            rest2 = rest[1:]
            if not rest2:
                if method == "POST":
                    tbl = inst.create_table(tbl_id)
                    return self._send_json(200, tbl.to_dict())
                if method == "GET":
                    return self._send_json(200, inst.get_table(tbl_id).to_dict())
                if method == "DELETE":
                    inst.delete_table(tbl_id)
                    return self._send_json(200, {"deleted": tbl_id})
                return self._error(405, method)
            tbl = inst.get_table(tbl_id)
            section = rest2[0]
            rest3 = rest2[1:]
            if section == "families":
                if not rest3:
                    if method == "GET":
                        return self._send_json(200, {
                            "columnFamilies": [
                                {"name": cf.name, "maxVersions": cf.max_versions}
                                for cf in tbl.list_column_families()
                            ]
                        })
                    return self._error(405, method)
                fam_name = rest3[0]
                if method == "POST":
                    mv = int(query.get("maxVersions", ["0"])[0])
                    cf = tbl.create_column_family(fam_name, max_versions=mv)
                    return self._send_json(200, {"name": cf.name,
                                                  "maxVersions": cf.max_versions})
                if method == "DELETE":
                    tbl.delete_column_family(fam_name)
                    return self._send_json(200, {"deleted": fam_name})
                return self._error(405, method)
            if section == "rows":
                if not rest3:
                    # scan
                    if method == "GET":
                        start = query.get("start", [""])[0]
                        end = query.get("end", [""])[0]
                        prefix = query.get("prefix", [""])[0]
                        limit = int(query.get("limit", ["0"])[0])
                        fams_raw = query.get("families", [None])[0]
                        fams = fams_raw.split(",") if fams_raw else None
                        rows = tbl.scan_rows(start_key=start, end_key=end,
                                             prefix=prefix, limit=limit,
                                             families=fams)
                        return self._send_json(200, {"rows": rows})
                    return self._error(405, method)
                row_key = rest3[0]
                action = rest3[1] if len(rest3) > 1 else None
                if action == "mutate":
                    if method == "POST":
                        body = self._json_body()
                        mutations = []
                        for m in body.get("mutations", []):
                            mtype = m.get("type")
                            if mtype == "setCell":
                                val = m.get("value", b"")
                                if isinstance(val, str):
                                    val = val.encode("utf-8")
                                mutations.append(SetCell(
                                    family=m["family"],
                                    qualifier=m["qualifier"],
                                    value=val,
                                    timestamp_micros=m.get("timestampMicros"),
                                ))
                            elif mtype == "deleteCell":
                                mutations.append(DeleteCell(
                                    family=m["family"],
                                    qualifier=m["qualifier"],
                                    timestamp_micros=m.get("timestampMicros"),
                                ))
                            elif mtype == "deleteFromFamily":
                                mutations.append(DeleteFromFamily(family=m["family"]))
                            elif mtype == "deleteFromRow":
                                mutations.append(DeleteFromRow())
                        tbl.mutate_row(row_key, mutations)
                        return self._send_json(200, {"mutated": row_key})
                    return self._error(405, method)
                # read single row
                if method == "GET":
                    fams_raw = query.get("families", [None])[0]
                    fams = fams_raw.split(",") if fams_raw else None
                    row = tbl.read_row(row_key, families=fams)
                    if row is None:
                        return self._error(404, f"row not found: {row_key}")
                    return self._send_json(200, row)
                return self._error(405, method)
            return self._error(404, f"unknown bigtable section: {section}")

        # ----- bigquery -----
        def _route_bigquery(self, method, p, query):
            bq = services.bigquery
            if not p:
                return self._error(404, "expected /bigquery/...")
            if p[0] == "query":
                if method == "POST":
                    body = self._json_body()
                    sql = body.get("query", "")
                    rows = bq.query(sql)
                    return self._send_json(200, {"rows": rows, "totalRows": len(rows)})
                return self._error(405, method)
            if p[0] != "datasets":
                return self._error(404, "expected /bigquery/datasets/...")
            rest = p[1:]
            if not rest:
                if method == "GET":
                    return self._send_json(200, {"datasets": bq.list_datasets()})
                return self._error(405, method)
            ds_id = rest[0]
            rest2 = rest[1:]
            if not rest2:
                if method == "POST":
                    return self._send_json(200, bq.create_dataset(ds_id))
                if method == "GET":
                    return self._send_json(200, bq.get_dataset(ds_id))
                if method == "DELETE":
                    dc = query.get("deleteContents", ["0"])[0] in ("1", "true")
                    bq.delete_dataset(ds_id, delete_contents=dc)
                    return self._send_json(200, {"deleted": ds_id})
                return self._error(405, method)
            if rest2[0] != "tables":
                return self._error(404, "expected /bigquery/datasets/<ds>/tables/...")
            rest3 = rest2[1:]
            if not rest3:
                if method == "GET":
                    return self._send_json(200, {"tables": bq.list_tables(ds_id)})
                return self._error(405, method)
            tbl_id = rest3[0]
            rest4 = rest3[1:]
            if not rest4:
                if method == "POST":
                    body = self._json_body()
                    schema = body.get("schema", body.get("fields", []))
                    tbl = bq.create_table(ds_id, tbl_id, schema)
                    return self._send_json(200, tbl.to_dict())
                if method == "GET":
                    return self._send_json(200, bq.get_table(ds_id, tbl_id).to_dict())
                if method == "DELETE":
                    bq.delete_table(ds_id, tbl_id)
                    return self._send_json(200, {"deleted": f"{ds_id}.{tbl_id}"})
                return self._error(405, method)
            action = rest4[0]
            if action == "insertAll":
                if method == "POST":
                    body = self._json_body()
                    rows = body.get("rows", body if isinstance(body, list) else [])
                    result = bq.insert_all(ds_id, tbl_id, rows)
                    return self._send_json(200, result)
                return self._error(405, method)
            return self._error(404, f"unknown bigquery action: {action}")

    return Handler


class OpenGCPServer:
    """Wraps a ThreadingHTTPServer and exposes the underlying services."""

    def __init__(self, host="127.0.0.1", port=8085, data_dir=None,
                 services: Services = None):
        self.services = services or Services(data_dir=data_dir)
        handler = _make_handler(self.services)
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.host, self.port = self.httpd.server_address
        self._thread = None

    @property
    def base_url(self):
        return f"http://{self.host}:{self.port}"

    def start_background(self):
        self._thread = threading.Thread(target=self.httpd.serve_forever,
                                        daemon=True)
        self._thread.start()
        return self

    def serve_forever(self):
        self.httpd.serve_forever()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread:
            self._thread.join(timeout=5)
