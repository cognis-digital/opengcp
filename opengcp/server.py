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

  IAM
    POST   /iam/roles/<roleId>                          create custom role (json body)
    GET    /iam/roles                                   list roles
    GET    /iam/roles/<roleId>                          get role
    PATCH  /iam/roles/<roleId>                          update role
    DELETE /iam/roles/<roleId>                          delete role
    POST   /iam/resources/<name...>                     register resource (?type=)
    GET    /iam/resources                               list resources
    GET    /iam/<resource...>/policy                    getIamPolicy
    POST   /iam/<resource...>/policy                    setIamPolicy (json body)
    POST   /iam/<resource...>/testPermissions           testIamPermissions (json body)

  Secret Manager
    POST   /secretmanager/secrets/<id>                  create secret (json body: {labels})
    GET    /secretmanager/secrets                       list secrets
    GET    /secretmanager/secrets/<id>                  get secret
    DELETE /secretmanager/secrets/<id>                  delete secret
    POST   /secretmanager/secrets/<id>/versions         add version (raw body = payload)
    GET    /secretmanager/secrets/<id>/versions         list versions
    GET    /secretmanager/secrets/<id>/versions/<v>     get version metadata
    GET    /secretmanager/secrets/<id>/versions/<v>:access  access (read) version payload
    POST   /secretmanager/secrets/<id>/versions/<v>:disable  disable version
    POST   /secretmanager/secrets/<id>/versions/<v>:enable   enable version
    POST   /secretmanager/secrets/<id>/versions/<v>:destroy  destroy version

  Cloud KMS
    POST   /kms/keyrings/<kr>                           create key ring
    GET    /kms/keyrings                                list key rings
    GET    /kms/keyrings/<kr>                           get key ring
    POST   /kms/keyrings/<kr>/keys/<key>                create crypto key (?purpose=)
    GET    /kms/keyrings/<kr>/keys                      list crypto keys
    GET    /kms/keyrings/<kr>/keys/<key>                get crypto key
    GET    /kms/keyrings/<kr>/keys/<key>/versions       list key versions
    POST   /kms/keyrings/<kr>/keys/<key>/encrypt        encrypt (json body: {plaintext, additionalAuthenticatedData})
    POST   /kms/keyrings/<kr>/keys/<key>/decrypt        decrypt (json body: {ciphertext, additionalAuthenticatedData})
    POST   /kms/keyrings/<kr>/keys/<key>/generateDataKey  generate DEK

  Cloud Logging
    POST   /logging/entries:write                       write log entries (json body: {entries, logName})
    GET    /logging/entries                             list entries (?logName=&severityMin=&filter=&pageSize=)
    GET    /logging/logs                                list log names
    DELETE /logging/logs/<logName...>                   delete a log
    GET    /logging/entries:tail                        tail recent entries (?n=)

  Cloud Monitoring
    POST   /monitoring/metricDescriptors                create metric descriptor
    GET    /monitoring/metricDescriptors                list metric descriptors
    GET    /monitoring/metricDescriptors/<type...>      get descriptor
    DELETE /monitoring/metricDescriptors/<type...>      delete descriptor
    POST   /monitoring/timeSeries                       write time-series
    GET    /monitoring/timeSeries                       list time-series (?metricType=&startTime=&endTime=&aligner=&period=)

  Identity Platform
    POST   /identityplatform/accounts:signUp            sign-up (json body)
    POST   /identityplatform/accounts:signIn            sign-in (json body)
    POST   /identityplatform/accounts:verify            verify ID token (json body)
    GET    /identityplatform/accounts/<uid>             get user
    PATCH  /identityplatform/accounts/<uid>             update user
    DELETE /identityplatform/accounts/<uid>             delete user
    GET    /identityplatform/accounts                   list users
    POST   /identityplatform/accounts/<uid>/customToken create custom token

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
    POST   /pubsub/topics/<topic>/publish            publish (json body; orderingKey optional)
    POST   /pubsub/subscriptions/<name>?topic=...    create subscription (?ackDeadline= ?deadLetterTopic= ?maxDeliveryAttempts=)
    GET    /pubsub/subscriptions                     list subscriptions
    GET    /pubsub/subscriptions/<name>              subscription stats
    DELETE /pubsub/subscriptions/<name>              delete subscription
    POST   /pubsub/subscriptions/<name>/pull         pull (?max=)
    POST   /pubsub/subscriptions/<name>/ack          ack (json {ackIds:[]})
    POST   /pubsub/subscriptions/<name>/nack         nack (json {ackIds:[]})
    POST   /pubsub/subscriptions/<name>/modifyAckDeadline  (json {ackId, seconds})

  Functions style
    GET    /functions                                list registered functions
    GET    /functions/invocations                    invocation log
    POST   /functions/<name>/invoke                  invoke HTTP-triggered function

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

  Cloud Tasks style
    POST   /tasks/queues/<queue>                     create queue (json body: retryConfig, rateLimits)
    GET    /tasks/queues                             list queues
    GET    /tasks/queues/<queue>                     get queue
    DELETE /tasks/queues/<queue>                     delete queue
    POST   /tasks/queues/<queue>/pause               pause queue
    POST   /tasks/queues/<queue>/resume              resume queue
    POST   /tasks/queues/<queue>/purge               purge all tasks
    POST   /tasks/queues/<queue>/tasks               create task (json body)
    GET    /tasks/queues/<queue>/tasks               list tasks
    GET    /tasks/queues/<queue>/tasks/<task>        get task
    DELETE /tasks/queues/<queue>/tasks/<task>        delete task

  Cloud Scheduler style
    POST   /scheduler/jobs                           create job (json body: name, schedule, description)
    GET    /scheduler/jobs                           list jobs
    GET    /scheduler/jobs/<job>                     get job
    DELETE /scheduler/jobs/<job>                     delete job
    POST   /scheduler/jobs/<job>/pause               pause job
    POST   /scheduler/jobs/<job>/resume              resume job
    POST   /scheduler/jobs/<job>/run                 run job now
    GET    /scheduler/jobs/<job>/history             execution history

  Cloud Run style
    POST   /cloudrun/services/<name>                 deploy service (handler registered in-process)
    GET    /cloudrun/services                        list services
    GET    /cloudrun/services/<name>                 get service
    DELETE /cloudrun/services/<name>                 delete service
    POST   /cloudrun/services/<name>/invoke          invoke service (raw body forwarded)
    GET    /cloudrun/services/<name>/invocations     invocation log

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
from . import tasks as tasks_mod
from . import scheduler as scheduler_mod
from . import cloudrun as cloudrun_mod
from . import iam as iam_mod
from . import secretmanager as secretmanager_mod
from . import kms as kms_mod
from . import logging_service as logging_mod
from . import monitoring as monitoring_mod
from . import identityplatform as identityplatform_mod
from .storage import ObjectStorage
from .firestore import DocumentStore
from .pubsub import PubSub, DeadLetterPolicy
from .functions import FunctionRunner
from .datastore import DatastoreDB, Key as DSKey
from .bigtable import BigtableAdmin, SetCell, DeleteCell, DeleteFromFamily, DeleteFromRow
from .bigquery import BigQueryDB
from .tasks import CloudTasks
from .scheduler import CloudScheduler
from .cloudrun import CloudRun
from .iam import IAMService
from .secretmanager import SecretManager
from .kms import KMSService
from .logging_service import LoggingService
from .monitoring import MonitoringService
from .identityplatform import IdentityPlatform


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
            self.iam = IAMService(path=os.path.join(data_dir, "iam.db"))
            self.secretmanager = SecretManager(
                path=os.path.join(data_dir, "secretmanager.db"))
            self.logging = LoggingService(path=os.path.join(data_dir, "logging.db"))
            self.monitoring = MonitoringService(
                path=os.path.join(data_dir, "monitoring.db"))
            self.identityplatform = IdentityPlatform(
                path=os.path.join(data_dir, "identityplatform.db"))
        else:
            self.storage = ObjectStorage(root=None)
            self.firestore = DocumentStore(path=None)
            self.datastore = DatastoreDB(path=None)
            self.bigquery = BigQueryDB(path=None)
            self.iam = IAMService()
            self.secretmanager = SecretManager()
            self.logging = LoggingService()
            self.monitoring = MonitoringService()
            self.identityplatform = IdentityPlatform()
        self.pubsub = PubSub()
        self.functions = FunctionRunner(storage=self.storage, pubsub=self.pubsub)
        self.bigtable = BigtableAdmin()
        self.tasks = CloudTasks()
        self.scheduler = CloudScheduler()
        self.cloudrun = CloudRun()
        self.kms = KMSService()  # always in-memory


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
                                                               "/bigquery", "/tasks",
                                                               "/scheduler", "/cloudrun",
                                                               "/iam", "/secretmanager",
                                                               "/kms", "/logging",
                                                               "/monitoring",
                                                               "/identityplatform",
                                                               "/healthz"]})
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
                if head == "tasks":
                    return self._route_tasks(method, parts[1:], query)
                if head == "scheduler":
                    return self._route_scheduler(method, parts[1:], query)
                if head == "cloudrun":
                    return self._route_cloudrun(method, parts[1:], query)
                if head == "iam":
                    return self._route_iam(method, parts[1:], query)
                if head == "secretmanager":
                    return self._route_secretmanager(method, parts[1:], query)
                if head == "kms":
                    return self._route_kms(method, parts[1:], query)
                if head == "logging":
                    return self._route_logging(method, parts[1:], query)
                if head == "monitoring":
                    return self._route_monitoring(method, parts[1:], query)
                if head == "identityplatform":
                    return self._route_identityplatform(method, parts[1:], query)
                return self._error(404, f"unknown service: {head}")
            except (storage_mod.StorageError, firestore_mod.FirestoreError,
                    pubsub_mod.PubSubError,
                    datastore_mod.DatastoreError,
                    bigtable_mod.BigtableError,
                    bigquery_mod.BigQueryError,
                    tasks_mod.TasksError,
                    scheduler_mod.SchedulerError,
                    cloudrun_mod.CloudRunError,
                    iam_mod.IAMError,
                    secretmanager_mod.SecretManagerError,
                    kms_mod.KMSError,
                    logging_mod.LoggingError,
                    monitoring_mod.MonitoringError,
                    identityplatform_mod.AuthError) as exc:
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
                    ordering_key = body.get("orderingKey", "")
                    mid = ps.publish(topic, data, attributes=attrs,
                                     ordering_key=ordering_key)
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
                        dl_topic = query.get("deadLetterTopic", [None])[0]
                        dl_max = int(query.get("maxDeliveryAttempts", ["5"])[0])
                        dl_policy = (
                            DeadLetterPolicy(dl_topic, dl_max)
                            if dl_topic else None
                        )
                        ps.create_subscription(name, topic, ack_deadline=ad,
                                               dead_letter_policy=dl_policy)
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
                if action == "modifyAckDeadline" and method == "POST":
                    body = self._json_body()
                    ack_id = body.get("ackId", "")
                    seconds = float(body.get("seconds", 10))
                    ok = ps.modify_ack_deadline(name, ack_id, seconds)
                    return self._send_json(200, {"modified": ok})
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
            # /functions/<name>/invoke  — HTTP-triggered function invocation
            if len(p) == 2 and p[1] == "invoke" and method == "POST":
                fn_name = p[0]
                body = self._read_body()
                request = {
                    "method": method,
                    "path": "/",
                    "headers": dict(self.headers),
                    "body": body,
                    "queryParams": dict(query),
                }
                inv = fr.fire_http(fn_name, request)
                if inv is None:
                    return self._error(404, f"no HTTP function: {fn_name}")
                result = inv.result
                if isinstance(result, dict):
                    status = result.get("status", 200)
                    resp_body = result.get("body", b"")
                    if isinstance(resp_body, str):
                        resp_body = resp_body.encode("utf-8")
                    ct = result.get("headers", {}).get("Content-Type",
                                                       "application/json")
                    return self._send_raw(status, resp_body, content_type=ct)
                if inv.ok:
                    return self._send_json(200, {"result": str(result) if result is not None else None})
                return self._error(500, inv.error or "function error")
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

        # ----- Cloud Tasks -----
        def _route_tasks(self, method, p, query):
            ct = services.tasks
            if not p or p[0] != "queues":
                return self._error(404, "expected /tasks/queues/...")
            rest = p[1:]
            if not rest:
                if method == "GET":
                    return self._send_json(200, {"queues": ct.list_queues()})
                return self._error(405, method)
            queue_name = rest[0]
            rest2 = rest[1:]
            if not rest2:
                if method == "POST":
                    body = self._json_body()
                    from .tasks import RetryConfig, RateLimits
                    import re as _re

                    def _to_snake(name):
                        return _re.sub(r'([A-Z])', lambda m: '_' + m.group(1).lower(), name)

                    rc_data = {_to_snake(k): v
                               for k, v in body.get("retryConfig", {}).items()}
                    rl_data = {_to_snake(k): v
                               for k, v in body.get("rateLimits", {}).items()}
                    rc = RetryConfig(**{k: v for k, v in rc_data.items()
                                       if k in RetryConfig.__dataclass_fields__}) if rc_data else None
                    rl = RateLimits(**{k: v for k, v in rl_data.items()
                                      if k in RateLimits.__dataclass_fields__}) if rl_data else None
                    q = ct.create_queue(queue_name, retry_config=rc, rate_limits=rl)
                    return self._send_json(200, q.to_dict())
                if method == "GET":
                    return self._send_json(200, ct.get_queue(queue_name).to_dict())
                if method == "DELETE":
                    ct.delete_queue(queue_name)
                    return self._send_json(200, {"deleted": queue_name})
                return self._error(405, method)
            action_or_tasks = rest2[0]
            if action_or_tasks == "pause" and method == "POST":
                ct.pause_queue(queue_name)
                return self._send_json(200, {"state": "PAUSED"})
            if action_or_tasks == "resume" and method == "POST":
                ct.resume_queue(queue_name)
                return self._send_json(200, {"state": "RUNNING"})
            if action_or_tasks == "purge" and method == "POST":
                n = ct.purge_queue(queue_name)
                return self._send_json(200, {"purged": n})
            if action_or_tasks == "tasks":
                rest3 = rest2[1:]
                if not rest3:
                    if method == "POST":
                        body = self._json_body()
                        raw_body = body.get("body", "")
                        if isinstance(raw_body, str):
                            raw_body = raw_body.encode("utf-8")
                        t = ct.create_task(
                            queue_name,
                            url=body.get("url"),
                            method=body.get("method", "POST"),
                            headers=body.get("headers", {}),
                            body=raw_body,
                            schedule_time=body.get("scheduleTime"),
                            name=body.get("name"),
                        )
                        return self._send_json(200, t.to_dict())
                    if method == "GET":
                        return self._send_json(200, {"tasks": ct.list_tasks(queue_name)})
                    return self._error(405, method)
                task_name = rest3[0]
                if method == "GET":
                    return self._send_json(200, ct.get_task(queue_name, task_name).to_dict())
                if method == "DELETE":
                    ct.delete_task(queue_name, task_name)
                    return self._send_json(200, {"deleted": task_name})
                return self._error(405, method)
            return self._error(404, f"unknown tasks action: {action_or_tasks}")

        # ----- Cloud Scheduler -----
        def _route_scheduler(self, method, p, query):
            sc = services.scheduler
            if not p or p[0] != "jobs":
                return self._error(404, "expected /scheduler/jobs/...")
            rest = p[1:]
            if not rest:
                if method == "GET":
                    return self._send_json(200, {"jobs": sc.list_jobs()})
                if method == "POST":
                    body = self._json_body()
                    name = body.get("name")
                    schedule = body.get("schedule")
                    if not name or not schedule:
                        return self._error(400, "missing name or schedule")
                    job = sc.create_job(name, schedule,
                                        description=body.get("description", ""))
                    return self._send_json(200, job.to_dict())
                return self._error(405, method)
            job_name = rest[0]
            rest2 = rest[1:]
            if not rest2:
                if method == "GET":
                    return self._send_json(200, sc.get_job(job_name).to_dict())
                if method == "DELETE":
                    sc.delete_job(job_name)
                    return self._send_json(200, {"deleted": job_name})
                return self._error(405, method)
            action = rest2[0]
            if action == "pause" and method == "POST":
                sc.pause_job(job_name)
                return self._send_json(200, {"state": "PAUSED"})
            if action == "resume" and method == "POST":
                sc.resume_job(job_name)
                return self._send_json(200, {"state": "ENABLED"})
            if action == "run" and method == "POST":
                sc.run_now(job_name)
                return self._send_json(200, {"status": "dispatched"})
            if action == "history" and method == "GET":
                return self._send_json(200, {"history": sc.job_history(job_name)})
            return self._error(404, f"unknown scheduler action: {action}")

        # ----- Cloud Run -----
        def _route_cloudrun(self, method, p, query):
            cr = services.cloudrun
            if not p or p[0] != "services":
                return self._error(404, "expected /cloudrun/services/...")
            rest = p[1:]
            if not rest:
                if method == "GET":
                    return self._send_json(200, {"services": cr.list_services()})
                return self._error(405, method)
            svc_name = rest[0]
            rest2 = rest[1:]
            if not rest2:
                if method == "GET":
                    return self._send_json(200, cr.get_service(svc_name).to_dict())
                if method == "DELETE":
                    cr.delete_service(svc_name)
                    return self._send_json(200, {"deleted": svc_name})
                # POST to /cloudrun/services/<name> without sub-path = describe
                return self._error(405, method)
            action = rest2[0]
            if action == "invoke" and method == "POST":
                body = self._read_body()
                ct_header = self.headers.get("Content-Type", "application/octet-stream")
                resp = cr.invoke(
                    svc_name,
                    method="POST",
                    path="/",
                    headers={"Content-Type": ct_header},
                    body=body,
                    query_params=dict(query),
                )
                return self._send_raw(resp["status"], resp["body"],
                                      content_type=resp.get("headers", {}).get(
                                          "Content-Type", "application/octet-stream"))
            if action == "invocations" and method == "GET":
                return self._send_json(200, {"invocations": cr.invocations(svc_name)})
            return self._error(404, f"unknown cloudrun action: {action}")

        # ----- IAM -----
        def _route_iam(self, method, p, query):
            iam = services.iam
            if not p:
                return self._error(404, "expected /iam/...")
            section = p[0]
            rest = p[1:]

            if section == "roles":
                if not rest:
                    if method == "GET":
                        return self._send_json(200, {"roles": iam.list_roles()})
                    return self._error(405, method)
                # role_id may contain slashes (e.g. "roles/myapp.reader")
                role_id = "/".join(rest)
                if method == "POST":
                    body = self._json_body()
                    role = iam.create_role(
                        role_id,
                        title=body.get("title", ""),
                        description=body.get("description", ""),
                        permissions=body.get("permissions", []),
                        stage=body.get("stage", "ALPHA"),
                    )
                    return self._send_json(200, role)
                if method == "GET":
                    return self._send_json(200, iam.get_role(role_id))
                if method == "PATCH":
                    body = self._json_body()
                    role = iam.update_role(
                        role_id,
                        title=body.get("title"),
                        description=body.get("description"),
                        permissions=body.get("permissions"),
                    )
                    return self._send_json(200, role)
                if method == "DELETE":
                    iam.delete_role(role_id)
                    return self._send_json(200, {"deleted": role_id})
                return self._error(405, method)

            if section == "resources":
                if not rest:
                    if method == "GET":
                        return self._send_json(200,
                                               {"resources": iam.list_resources()})
                    return self._error(405, method)
                # /iam/resources/<name...>  POST = register
                resource_name = "/".join(rest)
                if method == "POST":
                    rtype = query.get("type", ["generic"])[0]
                    return self._send_json(200,
                                           iam.register_resource(resource_name, rtype))
                return self._error(405, method)

            # /iam/<resource...>/policy  or  /iam/<resource...>/testPermissions
            # The resource path is everything up to the last segment
            if rest and rest[-1] in ("policy", "testPermissions"):
                action = rest[-1]
                resource = "/".join([section] + list(rest[:-1]))
                if action == "policy":
                    if method == "GET":
                        return self._send_json(200, iam.get_iam_policy(resource))
                    if method == "POST":
                        body = self._json_body()
                        bindings = body.get("bindings", [])
                        return self._send_json(200,
                                               iam.set_iam_policy(resource, bindings))
                    return self._error(405, method)
                if action == "testPermissions" and method == "POST":
                    body = self._json_body()
                    principal = body.get("principal", "")
                    permissions = body.get("permissions", [])
                    allowed = iam.test_iam_permissions(resource, principal,
                                                       permissions)
                    return self._send_json(200, {"permissions": allowed})
                return self._error(404, "bad IAM route")
            # no sub-action — treat as /iam/<resource...>/policy GET shortcut
            resource = "/".join([section] + list(rest))
            if method == "GET":
                return self._send_json(200, iam.get_iam_policy(resource))
            return self._error(404, f"unknown IAM route")

        # ----- Secret Manager -----
        def _route_secretmanager(self, method, p, query):
            sm = services.secretmanager
            if not p or p[0] != "secrets":
                return self._error(404, "expected /secretmanager/secrets/...")
            rest = p[1:]
            if not rest:
                if method == "GET":
                    return self._send_json(200, {"secrets": sm.list_secrets()})
                return self._error(405, method)
            secret_id = rest[0]
            rest2 = rest[1:]
            if not rest2:
                if method == "POST":
                    body = self._json_body()
                    secret = sm.create_secret(
                        secret_id, labels=body.get("labels"))
                    return self._send_json(200, secret)
                if method == "GET":
                    return self._send_json(200, sm.get_secret(secret_id))
                if method == "DELETE":
                    sm.delete_secret(secret_id)
                    return self._send_json(200, {"deleted": secret_id})
                return self._error(405, method)
            if rest2[0] != "versions":
                return self._error(404, "expected /secretmanager/secrets/<id>/versions/...")
            rest3 = rest2[1:]
            if not rest3:
                if method == "POST":
                    payload = self._read_body()
                    ver = sm.add_version(secret_id, payload)
                    return self._send_json(200, ver)
                if method == "GET":
                    return self._send_json(200,
                                           {"versions": sm.list_versions(secret_id)})
                return self._error(405, method)
            # /versions/<version> or /versions/<version>:<action>
            ver_raw = rest3[0]
            # detect colon-actions like "1:access", "latest:access"
            if ":" in ver_raw:
                ver_part, _, action = ver_raw.partition(":")
            else:
                ver_part = ver_raw
                action = ""
            if action == "access":
                if method == "GET":
                    payload = sm.access_version(secret_id, ver_part)
                    import base64 as _b64
                    return self._send_json(200, {
                        "name": f"projects/local/secrets/{secret_id}/versions/{ver_part}",
                        "payload": _b64.b64encode(payload).decode("ascii"),
                    })
                return self._error(405, method)
            if action == "disable":
                if method == "POST":
                    return self._send_json(200,
                                           sm.disable_version(secret_id, ver_part))
                return self._error(405, method)
            if action == "enable":
                if method == "POST":
                    return self._send_json(200,
                                           sm.enable_version(secret_id, ver_part))
                return self._error(405, method)
            if action == "destroy":
                if method == "POST":
                    return self._send_json(200,
                                           sm.destroy_version(secret_id, ver_part))
                return self._error(405, method)
            # no action — version metadata
            if method == "GET":
                return self._send_json(200,
                                       sm.get_version(secret_id, ver_part))
            return self._error(405, method)

        # ----- Cloud KMS -----
        def _route_kms(self, method, p, query):
            kms = services.kms
            if not p or p[0] != "keyrings":
                return self._error(404, "expected /kms/keyrings/...")
            rest = p[1:]
            if not rest:
                if method == "GET":
                    return self._send_json(200,
                                           {"keyRings": kms.list_key_rings()})
                return self._error(405, method)
            kr_id = rest[0]
            rest2 = rest[1:]
            if not rest2:
                if method == "POST":
                    return self._send_json(200, kms.create_key_ring(kr_id))
                if method == "GET":
                    return self._send_json(200, kms.get_key_ring(kr_id))
                return self._error(405, method)
            if rest2[0] != "keys":
                return self._error(404, "expected /kms/keyrings/<kr>/keys/...")
            rest3 = rest2[1:]
            if not rest3:
                if method == "GET":
                    return self._send_json(200,
                                           {"cryptoKeys": kms.list_crypto_keys(kr_id)})
                return self._error(405, method)
            key_id = rest3[0]
            rest4 = rest3[1:]
            if not rest4:
                if method == "POST":
                    purpose = query.get("purpose", ["ENCRYPT_DECRYPT"])[0]
                    return self._send_json(200,
                                           kms.create_crypto_key(kr_id, key_id,
                                                                   purpose=purpose))
                if method == "GET":
                    return self._send_json(200,
                                           kms.get_crypto_key(kr_id, key_id))
                return self._error(405, method)
            action = rest4[0]
            if action == "versions" and method == "GET":
                return self._send_json(200, {
                    "cryptoKeyVersions":
                        kms.list_crypto_key_versions(kr_id, key_id)
                })
            if action == "encrypt" and method == "POST":
                body = self._json_body()
                import base64 as _b64
                pt_raw = body.get("plaintext", "")
                aad_raw = body.get("additionalAuthenticatedData", "")
                try:
                    plaintext = _b64.b64decode(pt_raw)
                except Exception:
                    plaintext = pt_raw.encode("utf-8") if isinstance(pt_raw, str) else pt_raw
                try:
                    aad = _b64.b64decode(aad_raw) if aad_raw else b""
                except Exception:
                    aad = aad_raw.encode("utf-8") if isinstance(aad_raw, str) else b""
                result = kms.encrypt(kr_id, key_id, plaintext, aad)
                return self._send_json(200, result)
            if action == "decrypt" and method == "POST":
                body = self._json_body()
                import base64 as _b64
                ct = body.get("ciphertext", "")
                aad_raw = body.get("additionalAuthenticatedData", "")
                try:
                    aad = _b64.b64decode(aad_raw) if aad_raw else b""
                except Exception:
                    aad = aad_raw.encode("utf-8") if isinstance(aad_raw, str) else b""
                plaintext = kms.decrypt(kr_id, key_id, ct, aad)
                return self._send_json(200, {
                    "plaintext": _b64.b64encode(plaintext).decode("ascii")
                })
            if action == "generateDataKey" and method == "POST":
                result = kms.generate_data_key(kr_id, key_id)
                return self._send_json(200, result)
            return self._error(404, f"unknown KMS action: {action}")

        # ----- Cloud Logging -----
        def _route_logging(self, method, p, query):
            lg = services.logging
            if not p:
                return self._error(404, "expected /logging/...")
            section = p[0]

            # /logging/entries:write  /logging/entries:tail  /logging/entries
            if section.startswith("entries"):
                if ":" in section:
                    _, _, action = section.partition(":")
                else:
                    action = ""
                if action == "write" and method == "POST":
                    body = self._json_body()
                    entries = body.get("entries", [])
                    log_name = body.get("logName", "")
                    ids = lg.write_entries(entries, log_name=log_name)
                    return self._send_json(200, {"insertedIds": ids})
                if action == "tail" and method == "GET":
                    n = int(query.get("n", ["100"])[0])
                    return self._send_json(200, {"entries": lg.tail(n)})
                if not action:
                    if method == "POST":
                        body = self._json_body()
                        entries = body.get("entries", [])
                        log_name = body.get("logName", "")
                        ids = lg.write_entries(entries, log_name=log_name)
                        return self._send_json(200, {"insertedIds": ids})
                    if method == "GET":
                        log_name = query.get("logName", [None])[0]
                        sev_min = query.get("severityMin", [None])[0]
                        filt = query.get("filter", [None])[0]
                        ps = int(query.get("pageSize", ["1000"])[0])
                        entries = lg.list_entries(log_name=log_name,
                                                  severity_min=sev_min,
                                                  filter_expr=filt,
                                                  page_size=ps)
                        return self._send_json(200, {"entries": entries})
                return self._error(405, method)

            if section == "logs":
                rest = p[1:]
                if not rest:
                    if method == "GET":
                        return self._send_json(200,
                                               {"logNames": lg.list_log_names()})
                    return self._error(405, method)
                log_name = "/".join(rest)
                if method == "DELETE":
                    n = lg.delete_log(log_name)
                    return self._send_json(200, {"deleted": n})
                return self._error(405, method)

            return self._error(404, f"unknown logging route: {section}")

        # ----- Cloud Monitoring -----
        def _route_monitoring(self, method, p, query):
            mon = services.monitoring
            if not p:
                return self._error(404, "expected /monitoring/...")
            section = p[0]
            rest = p[1:]

            if section == "metricDescriptors":
                if not rest:
                    if method == "GET":
                        return self._send_json(200, {
                            "metricDescriptors": mon.list_metric_descriptors()})
                    if method == "POST":
                        body = self._json_body()
                        desc = mon.create_metric_descriptor(
                            metric_type=body.get("type", ""),
                            display_name=body.get("displayName", ""),
                            description=body.get("description", ""),
                            value_type=body.get("valueType", "DOUBLE"),
                            metric_kind=body.get("metricKind", "GAUGE"),
                            unit=body.get("unit", "1"),
                            labels=body.get("labels"),
                        )
                        return self._send_json(200, desc)
                    return self._error(405, method)
                metric_type = "/".join(rest)
                if method == "GET":
                    return self._send_json(200,
                                           mon.get_metric_descriptor(metric_type))
                if method == "DELETE":
                    mon.delete_metric_descriptor(metric_type)
                    return self._send_json(200, {"deleted": metric_type})
                return self._error(405, method)

            if section == "timeSeries":
                if not rest:
                    if method == "POST":
                        body = self._json_body()
                        ts_list = body.get("timeSeries", body if isinstance(body, list) else [])
                        n = mon.write_time_series(ts_list)
                        return self._send_json(200, {"written": n})
                    if method == "GET":
                        mt = query.get("metricType", [None])[0]
                        st = query.get("startTime", [None])[0]
                        et = query.get("endTime", [None])[0]
                        aligner = query.get("aligner", ["ALIGN_NONE"])[0]
                        period = query.get("period", [None])[0]
                        ps = int(query.get("pageSize", ["1000"])[0])
                        ts = mon.list_time_series(
                            metric_type=mt,
                            start_time=float(st) if st else None,
                            end_time=float(et) if et else None,
                            aligner=aligner,
                            alignment_period=float(period) if period else None,
                            page_size=ps,
                        )
                        return self._send_json(200, {"timeSeries": ts})
                    return self._error(405, method)
                return self._error(404, "bad monitoring timeSeries route")

            return self._error(404, f"unknown monitoring route: {section}")

        # ----- Identity Platform -----
        def _route_identityplatform(self, method, p, query):
            auth = services.identityplatform
            if not p:
                return self._error(404, "expected /identityplatform/accounts/...")

            # p[0] may be "accounts", "accounts:signUp", "accounts:signIn", etc.
            first = p[0]
            if ":" in first:
                # colon-action form: /identityplatform/accounts:signUp
                base, _, action = first.partition(":")
                if base != "accounts":
                    return self._error(404, f"unknown segment: {first}")
                if action == "signUp" and method == "POST":
                    body = self._json_body()
                    result = auth.sign_up(
                        email=body.get("email", ""),
                        password=body.get("password", ""),
                        display_name=body.get("displayName", ""),
                    )
                    return self._send_json(200, result)
                if action == "signIn" and method == "POST":
                    body = self._json_body()
                    result = auth.sign_in(
                        email=body.get("email", ""),
                        password=body.get("password", ""),
                    )
                    return self._send_json(200, result)
                if action == "verify" and method == "POST":
                    body = self._json_body()
                    token = body.get("idToken", "")
                    payload = auth.verify_id_token(token)
                    return self._send_json(200, {"payload": payload})
                return self._error(404, f"unknown accounts action: {action}")

            if first != "accounts":
                return self._error(404, "expected /identityplatform/accounts/...")

            rest = p[1:]
            if not rest:
                # GET /identityplatform/accounts  → list users
                if method == "GET":
                    max_r = int(query.get("maxResults", ["1000"])[0])
                    return self._send_json(200, {"users": auth.list_users(max_r)})
                return self._error(405, method)

            # /identityplatform/accounts/<uid> and sub-paths
            uid = rest[0]
            rest2 = rest[1:]
            if not rest2:
                if method == "GET":
                    return self._send_json(200, auth.get_user(uid))
                if method == "PATCH":
                    body = self._json_body()
                    result = auth.update_user(
                        uid,
                        display_name=body.get("displayName"),
                        password=body.get("password"),
                        disabled=body.get("disabled"),
                    )
                    return self._send_json(200, result)
                if method == "DELETE":
                    auth.delete_user(uid)
                    return self._send_json(200, {"deleted": uid})
                return self._error(405, method)
            sub_action = rest2[0]
            if sub_action == "customToken" and method == "POST":
                body = self._json_body()
                token = auth.create_custom_token(
                    uid,
                    claims=body.get("claims"),
                    ttl=int(body.get("ttl", 3600)),
                )
                return self._send_json(200, {"customToken": token})
            return self._error(404, f"unknown identityplatform route: {sub_action}")

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
        # stop background threads in the new services
        try:
            self.services.tasks.stop()
        except Exception:
            pass
        try:
            self.services.scheduler.stop()
        except Exception:
            pass
