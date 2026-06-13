"""Extended Cloud Functions tests: HTTP trigger, firestore.write trigger."""

import pytest

from opengcp.functions import (FunctionRunner, FunctionError,
                               HTTP_TRIGGER, FIRESTORE_WRITE,
                               OBJECT_FINALIZE, PUBSUB_PUBLISH)
from opengcp.pubsub import PubSub
from opengcp.storage import ObjectStorage


# ----- HTTP trigger -----

def test_register_http_trigger():
    fr = FunctionRunner()
    fr.register("api", HTTP_TRIGGER, lambda req: {"status": 200, "body": b"ok"})
    fns = fr.list_functions()
    assert any(f["name"] == "api" and f["trigger"] == "http" for f in fns)


def test_fire_http_invocation():
    fr = FunctionRunner()
    captured = []

    def handler(req):
        captured.append(req)
        return {"status": 200, "body": b"hello"}

    fr.register("fn", HTTP_TRIGGER, handler)
    inv = fr.fire_http("fn", {
        "method": "POST",
        "path": "/test",
        "headers": {"Content-Type": "application/json"},
        "body": b'{"key":"val"}',
        "queryParams": {},
    })
    assert inv is not None
    assert inv.ok is True
    assert inv.result["status"] == 200
    assert len(captured) == 1
    assert captured[0]["path"] == "/test"


def test_fire_http_missing_function_returns_none():
    fr = FunctionRunner()
    inv = fr.fire_http("nonexistent", {})
    assert inv is None


def test_fire_http_wrong_trigger_returns_none():
    fr = FunctionRunner()
    fr.register("fn", OBJECT_FINALIZE, lambda e: None)
    inv = fr.fire_http("fn", {})
    assert inv is None


def test_fire_http_captures_error():
    fr = FunctionRunner()

    def boom(req):
        raise RuntimeError("HTTP handler failed")

    fr.register("fn", HTTP_TRIGGER, boom)
    inv = fr.fire_http("fn", {})
    assert inv is not None
    assert inv.ok is False
    assert "HTTP handler failed" in inv.error


def test_fire_http_recorded_in_log():
    fr = FunctionRunner()
    fr.register("fn", HTTP_TRIGGER, lambda req: None)
    fr.fire_http("fn", {"method": "GET", "path": "/", "headers": {},
                        "body": b"", "queryParams": {}})
    log = fr.invocations("fn")
    assert len(log) == 1
    assert log[0].event_type == "http"


def test_fire_http_returns_none_handler_ok():
    fr = FunctionRunner()
    fr.register("fn", HTTP_TRIGGER, lambda req: None)
    inv = fr.fire_http("fn", {})
    assert inv.ok is True
    assert inv.result is None


# ----- firestore.write trigger -----

def test_register_firestore_write_trigger():
    fr = FunctionRunner()
    fr.register("on_doc", FIRESTORE_WRITE, lambda e: None)
    fns = fr.list_functions()
    assert any(f["name"] == "on_doc" and f["trigger"] == "firestore.write"
               for f in fns)


def test_fire_firestore_write_create():
    fr = FunctionRunner()
    captured = []
    fr.register("on_doc", FIRESTORE_WRITE,
                lambda e: captured.append(e["operation"]))
    invs = fr.fire_firestore_write("users", "u1", "CREATE",
                                   {"name": "ada"}, old_data=None)
    assert len(invs) == 1
    assert invs[0].ok is True
    assert captured == ["CREATE"]


def test_fire_firestore_write_event_shape():
    fr = FunctionRunner()
    events = []
    fr.register("fn", FIRESTORE_WRITE, lambda e: events.append(e))
    fr.fire_firestore_write("col", "doc1", "UPDATE",
                            {"x": 2}, old_data={"x": 1})
    e = events[0]
    assert e["collection"] == "col"
    assert e["docId"] == "doc1"
    assert e["operation"] == "UPDATE"
    assert e["data"] == {"x": 2}
    assert e["oldData"] == {"x": 1}
    assert e["eventType"] == FIRESTORE_WRITE


def test_fire_firestore_write_delete():
    fr = FunctionRunner()
    captured = []
    fr.register("fn", FIRESTORE_WRITE,
                lambda e: captured.append((e["operation"], e["docId"])))
    fr.fire_firestore_write("items", "item42", "DELETE", {}, old_data={"x": 1})
    assert captured == [("DELETE", "item42")]


def test_fire_firestore_write_resource_filter():
    fr = FunctionRunner()
    hits = []
    fr.register("only_users", FIRESTORE_WRITE,
                lambda e: hits.append(e["collection"]), resource="users")
    fr.fire_firestore_write("orders", "o1", "CREATE", {})
    assert hits == []
    fr.fire_firestore_write("users", "u1", "CREATE", {})
    assert hits == ["users"]


def test_fire_firestore_write_captures_handler_error():
    fr = FunctionRunner()

    def boom(e):
        raise ValueError("db write handler crash")

    fr.register("fn", FIRESTORE_WRITE, boom)
    invs = fr.fire_firestore_write("c", "d", "CREATE", {})
    assert invs[0].ok is False
    assert "db write handler crash" in invs[0].error


def test_fire_firestore_write_multiple_handlers():
    fr = FunctionRunner()
    results = []
    fr.register("h1", FIRESTORE_WRITE, lambda e: results.append("h1"))
    fr.register("h2", FIRESTORE_WRITE, lambda e: results.append("h2"))
    fr.fire_firestore_write("c", "d", "CREATE", {})
    assert set(results) == {"h1", "h2"}


# ----- all triggers in invocation log -----

def test_all_trigger_types_appear_in_log():
    fr = FunctionRunner()
    fr.register("http_fn", HTTP_TRIGGER, lambda req: None)
    fr.register("obj_fn", OBJECT_FINALIZE, lambda e: None)
    fr.register("pub_fn", PUBSUB_PUBLISH, lambda e: None)
    fr.register("fs_fn", FIRESTORE_WRITE, lambda e: None)

    fr.fire_http("http_fn", {})
    fr.fire_object_finalize("bucket", "key")
    fr.fire_pubsub_publish("topic", b"data")
    fr.fire_firestore_write("col", "doc", "CREATE", {})

    log = fr.invocations()
    triggers = {i.event_type for i in log}
    assert triggers == {HTTP_TRIGGER, OBJECT_FINALIZE, PUBSUB_PUBLISH, FIRESTORE_WRITE}


# ----- invalid trigger still rejected -----

def test_unknown_trigger_still_rejected():
    fr = FunctionRunner()
    with pytest.raises(FunctionError):
        fr.register("fn", "unknown.trigger", lambda e: None)
