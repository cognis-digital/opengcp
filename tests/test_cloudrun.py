"""Tests for Cloud Run emulator (opengcp.cloudrun)."""

import threading
import time

import pytest

from opengcp.cloudrun import (CloudRun, ServiceConfig, CloudRunError,
                              ServiceNotFound, _normalize_response)


# ----- _normalize_response helper -----

def test_normalize_none():
    r = _normalize_response(None)
    assert r == {"status": 200, "headers": {}, "body": b""}


def test_normalize_dict():
    r = _normalize_response({"status": 201, "body": b"created"})
    assert r["status"] == 201
    assert r["body"] == b"created"


def test_normalize_str_body():
    r = _normalize_response({"status": 200, "body": "hello"})
    assert r["body"] == b"hello"


def test_normalize_bytes():
    r = _normalize_response(b"raw bytes")
    assert r == {"status": 200, "headers": {}, "body": b"raw bytes"}


def test_normalize_str():
    r = _normalize_response("plain text")
    assert r["body"] == b"plain text"


# ----- service lifecycle -----

def test_deploy_and_list_services():
    cr = CloudRun()
    cr.deploy("svc1", lambda req: None)
    cr.deploy("svc2", lambda req: {"status": 201})
    services = cr.list_services()
    assert [s["name"] for s in services] == ["svc1", "svc2"]


def test_get_service():
    cr = CloudRun()
    cr.deploy("svc", lambda req: None, region="us-central1")
    svc = cr.get_service("svc")
    assert svc.region == "us-central1"
    d = svc.to_dict()
    assert d["state"] == "ACTIVE"


def test_get_service_not_found():
    cr = CloudRun()
    with pytest.raises(ServiceNotFound):
        cr.get_service("nope")


def test_delete_service():
    cr = CloudRun()
    cr.deploy("svc", lambda req: None)
    cr.delete_service("svc")
    with pytest.raises(ServiceNotFound):
        cr.get_service("svc")


def test_redeploy_replaces_handler():
    cr = CloudRun()
    cr.deploy("svc", lambda req: {"body": b"v1"})
    cr.deploy("svc", lambda req: {"body": b"v2"})  # redeploy
    resp = cr.invoke("svc")
    assert resp["body"] == b"v2"


# ----- invocation -----

def test_invoke_returns_response():
    cr = CloudRun()

    def handler(req):
        return {"status": 200, "body": b"pong", "headers": {}}

    cr.deploy("echo", handler)
    resp = cr.invoke("echo", method="GET", path="/ping")
    assert resp["status"] == 200
    assert resp["body"] == b"pong"


def test_invoke_receives_request_shape():
    cr = CloudRun()
    captured = []

    def handler(req):
        captured.append(req)
        return None

    cr.deploy("svc", handler)
    cr.invoke("svc", method="POST", path="/test",
              headers={"Content-Type": "application/json"},
              body=b'{"key":"val"}')
    assert len(captured) == 1
    req = captured[0]
    assert req["method"] == "POST"
    assert req["path"] == "/test"
    assert req["body"] == b'{"key":"val"}'
    assert req["headers"]["Content-Type"] == "application/json"


def test_invoke_handler_exception_returns_500():
    cr = CloudRun()

    def boom(req):
        raise RuntimeError("handler exploded")

    cr.deploy("svc", boom)
    resp = cr.invoke("svc")
    assert resp["status"] == 500
    assert b"handler exploded" in resp["body"]


def test_invoke_increments_invocation_count():
    cr = CloudRun()
    cr.deploy("svc", lambda req: None)
    cr.invoke("svc")
    cr.invoke("svc")
    svc = cr.get_service("svc")
    assert svc.invocation_count == 2


def test_invocations_log():
    cr = CloudRun()
    cr.deploy("svc", lambda req: {"status": 201})
    cr.invoke("svc", path="/a")
    cr.invoke("svc", path="/b")
    log = cr.invocations("svc")
    assert len(log) == 2
    assert all(r["ok"] for r in log)
    assert log[0]["path"] == "/a"
    assert log[0]["status"] == 201


def test_invocations_log_captures_error():
    cr = CloudRun()
    cr.deploy("svc", lambda req: (_ for _ in ()).throw(ValueError("bad")))
    cr.invoke("svc")
    log = cr.invocations("svc")
    assert log[0]["ok"] is False
    assert log[0]["error"] is not None


def test_invoke_missing_service():
    cr = CloudRun()
    with pytest.raises(ServiceNotFound):
        cr.invoke("nope")


def test_invoke_with_config():
    cr = CloudRun()
    cfg = ServiceConfig(max_concurrency=10, timeout=5.0)
    cr.deploy("svc", lambda req: None, config=cfg)
    svc = cr.get_service("svc")
    assert svc.config.max_concurrency == 10


def test_concurrency_semaphore_allows_parallel():
    """Multiple threads can invoke simultaneously up to max_concurrency."""
    cr = CloudRun()
    barrier = threading.Barrier(3)
    results = []

    def slow_handler(req):
        barrier.wait(timeout=3)
        return {"status": 200}

    cr.deploy("svc", slow_handler, config=ServiceConfig(max_concurrency=5))

    threads = [threading.Thread(target=lambda: results.append(cr.invoke("svc")))
               for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(results) == 3
    assert all(r["status"] == 200 for r in results)


def test_service_to_dict_shape():
    cr = CloudRun()
    cr.deploy("svc", lambda req: None)
    d = cr.get_service("svc").to_dict()
    required_keys = {"name", "region", "state", "createTime",
                     "invocationCount", "lastStatus", "config"}
    assert required_keys.issubset(d.keys())
    assert d["config"]["maxConcurrency"] == 80  # default
