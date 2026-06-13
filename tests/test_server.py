"""End-to-end HTTP tests: start the real server in-process and round-trip
data through every service over the wire."""

import base64
import json
import urllib.error
import urllib.request

import pytest

from opengcp.server import OpenGCPServer


@pytest.fixture()
def server():
    srv = OpenGCPServer(host="127.0.0.1", port=0, data_dir=None).start_background()
    yield srv
    srv.stop()


def req(srv, method, path, body=None, headers=None, raw=False):
    url = srv.base_url + path
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        elif isinstance(body, bytes):
            data = body
        else:
            data = str(body).encode("utf-8")
    r = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(r) as resp:
            payload = resp.read()
            if raw:
                return resp.status, payload
            return resp.status, json.loads(payload)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_healthz(server):
    code, body = req(server, "GET", "/healthz")
    assert code == 200 and body["status"] == "ok"


def test_storage_http_roundtrip(server):
    code, _ = req(server, "POST", "/storage/b/mybucket")
    assert code == 200
    code, body = req(server, "GET", "/storage/b")
    assert [b["name"] for b in body["items"]] == ["mybucket"]
    # upload raw bytes
    code, meta = req(server, "POST", "/storage/b/mybucket/o/path/to/file.txt",
                     body=b"hello-http", headers={"Content-Type": "text/plain"})
    assert code == 200 and meta["size"] == 10
    # download raw
    code, data = req(server, "GET", "/storage/b/mybucket/o/path/to/file.txt", raw=True)
    assert code == 200 and data == b"hello-http"
    # metadata
    code, m = req(server, "GET", "/storage/b/mybucket/o/path/to/file.txt?meta=1")
    assert m["contentType"] == "text/plain"
    # list
    code, lst = req(server, "GET", "/storage/b/mybucket/o")
    assert lst["items"][0]["name"] == "path/to/file.txt"
    # delete
    code, _ = req(server, "DELETE", "/storage/b/mybucket/o/path/to/file.txt")
    assert code == 200
    code, _ = req(server, "GET", "/storage/b/mybucket/o/path/to/file.txt", raw=True)
    assert code == 404


def test_firestore_http_roundtrip(server):
    code, body = req(server, "POST", "/firestore/users", body={"name": "ada", "age": 36})
    assert code == 200
    doc_id = body["id"]
    code, got = req(server, "GET", f"/firestore/users/{doc_id}")
    assert got["data"]["name"] == "ada"
    # patch
    code, patched = req(server, "PATCH", f"/firestore/users/{doc_id}", body={"age": 37})
    assert patched["data"]["age"] == 37
    # put
    code, _ = req(server, "PUT", "/firestore/users/u2", body={"name": "linus"})
    code, lst = req(server, "GET", "/firestore/users")
    assert len(lst["documents"]) == 2
    # query
    code, q = req(server, "GET", "/firestore/users?field=name&op===&value=%22linus%22")
    assert len(q["documents"]) == 1
    assert q["documents"][0]["data"]["name"] == "linus"
    # delete
    code, _ = req(server, "DELETE", f"/firestore/users/{doc_id}")
    code, got = req(server, "GET", f"/firestore/users/{doc_id}")
    assert code == 404


def test_pubsub_http_roundtrip(server):
    req(server, "POST", "/pubsub/topics/events")
    req(server, "POST", "/pubsub/subscriptions/sub1?topic=events")
    code, body = req(server, "POST", "/pubsub/topics/events/publish",
                     body={"data": "aGVsbG8=", "dataEncoding": "base64",
                           "attributes": {"x": "y"}})
    assert code == 200
    code, pulled = req(server, "POST", "/pubsub/subscriptions/sub1/pull?max=10")
    assert len(pulled["receivedMessages"]) == 1
    msg = pulled["receivedMessages"][0]
    assert base64.b64decode(msg["message"]["data"]) == b"hello"
    assert msg["message"]["attributes"] == {"x": "y"}
    ack_id = msg["ackId"]
    code, acked = req(server, "POST", "/pubsub/subscriptions/sub1/ack",
                      body={"ackIds": [ack_id]})
    assert acked["acked"] == 1
    code, stats = req(server, "GET", "/pubsub/subscriptions/sub1")
    assert stats["outstanding"] == 0


def test_object_finalize_triggers_function(server):
    # register a function directly on the in-process runner
    seen = []
    server.services.functions.register(
        "on_upload", "object.finalize",
        lambda e: seen.append((e["bucket"], e["name"])), resource="b")
    req(server, "POST", "/storage/b/b")
    req(server, "POST", "/storage/b/b/o/trigger.txt", body=b"data")
    assert seen == [("b", "trigger.txt")]
    code, body = req(server, "GET", "/functions/invocations")
    assert any(i["function"] == "on_upload" and i["ok"] for i in body["invocations"])


def test_pubsub_publish_triggers_function(server):
    seen = []
    server.services.functions.register(
        "on_msg", "pubsub.publish",
        lambda e: seen.append(e["data"]), resource="t")
    req(server, "POST", "/pubsub/topics/t")
    req(server, "POST", "/pubsub/topics/t/publish", body={"data": "boom"})
    assert seen == [b"boom"]


def test_functions_listing(server):
    server.services.functions.register("f", "object.finalize", lambda e: None)
    code, body = req(server, "GET", "/functions")
    assert any(f["name"] == "f" for f in body["functions"])


def test_404_unknown_service(server):
    code, body = req(server, "GET", "/nope")
    assert code == 404


def test_root_lists_endpoints(server):
    code, body = req(server, "GET", "/")
    assert "/storage" in body["endpoints"]
