"""Single local HTTP server exposing all opengcp services under path prefixes.

Path layout (all JSON unless noted):

  Storage   (GCS-style)
    POST   /storage/b/<bucket>                       create bucket
    GET    /storage/b                                list buckets
    GET    /storage/b/<bucket>                       get bucket
    DELETE /storage/b/<bucket>                       delete bucket
    POST   /storage/b/<bucket>/o/<name...>           upload object (raw body)
    GET    /storage/b/<bucket>/o/<name...>           download object (raw body)
    GET    /storage/b/<bucket>/o/<name...>?meta=1    object metadata (json)
    GET    /storage/b/<bucket>/o                      list objects (?prefix=)
    DELETE /storage/b/<bucket>/o/<name...>            delete object

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
from .storage import ObjectStorage
from .firestore import DocumentStore
from .pubsub import PubSub
from .functions import FunctionRunner


class Services:
    """Container wiring the four services together."""

    def __init__(self, data_dir=None):
        if data_dir:
            import os
            os.makedirs(data_dir, exist_ok=True)
            self.storage = ObjectStorage(root=os.path.join(data_dir, "storage"))
            self.firestore = DocumentStore(path=os.path.join(data_dir, "firestore.db"))
        else:
            self.storage = ObjectStorage(root=None)
            self.firestore = DocumentStore(path=None)
        self.pubsub = PubSub()
        self.functions = FunctionRunner(storage=self.storage, pubsub=self.pubsub)


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
                return self._error(404, f"unknown service: {head}")
            except (storage_mod.StorageError, firestore_mod.FirestoreError,
                    pubsub_mod.PubSubError) as exc:
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
                    return self._send_json(200, s.create_bucket(bucket))
                if method == "GET":
                    return self._send_json(200, s.get_bucket(bucket))
                if method == "DELETE":
                    s.delete_bucket(bucket)
                    return self._send_json(200, {"deleted": bucket})
                return self._error(405, method)
            if rest[0] != "o":
                return self._error(404, "expected /storage/b/<bucket>/o/...")
            obj_parts = rest[1:]
            if not obj_parts:
                if method == "GET":
                    prefix = query.get("prefix", [""])[0]
                    items = [m.to_dict() for m in s.list_objects(bucket, prefix)]
                    return self._send_json(200, {"items": items})
                return self._error(405, method)
            name = "/".join(obj_parts)
            if method == "POST":
                data = self._read_body()
                ct = self.headers.get("Content-Type", "application/octet-stream")
                meta = s.upload(bucket, name, data, content_type=ct)
                services.functions.fire_object_finalize(bucket, name, size=meta.size)
                return self._send_json(200, meta.to_dict())
            if method == "GET":
                if query.get("meta", ["0"])[0] in ("1", "true"):
                    return self._send_json(200, s.stat(bucket, name).to_dict())
                data = s.download(bucket, name)
                ct = s.stat(bucket, name).content_type
                return self._send_raw(200, data, content_type=ct)
            if method == "DELETE":
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
