import pytest

from opengcp.functions import (FunctionRunner, FunctionError, OBJECT_FINALIZE,
                               PUBSUB_PUBLISH)
from opengcp.storage import ObjectStorage
from opengcp.pubsub import PubSub


def test_register_and_list():
    fr = FunctionRunner()
    fr.register("f1", OBJECT_FINALIZE, lambda e: None)
    names = [f["name"] for f in fr.list_functions()]
    assert names == ["f1"]


def test_unknown_trigger_rejected():
    fr = FunctionRunner()
    with pytest.raises(FunctionError):
        fr.register("f", "bad.trigger", lambda e: None)


def test_non_callable_rejected():
    fr = FunctionRunner()
    with pytest.raises(FunctionError):
        fr.register("f", OBJECT_FINALIZE, 123)


def test_object_finalize_dispatch():
    fr = FunctionRunner()
    captured = []
    fr.register("f", OBJECT_FINALIZE, lambda e: captured.append(e) or "done")
    invs = fr.fire_object_finalize("bucket", "key", size=10)
    assert len(invs) == 1
    assert invs[0].ok is True
    assert invs[0].result == "done"
    assert captured[0]["bucket"] == "bucket"
    assert captured[0]["name"] == "key"
    assert captured[0]["size"] == 10


def test_resource_filter():
    fr = FunctionRunner()
    hits = []
    fr.register("only_a", OBJECT_FINALIZE,
                lambda e: hits.append("a"), resource="bucket-a")
    fr.fire_object_finalize("bucket-b", "k")
    assert hits == []
    fr.fire_object_finalize("bucket-a", "k")
    assert hits == ["a"]


def test_handler_error_captured():
    fr = FunctionRunner()

    def boom(event):
        raise RuntimeError("kaboom")

    fr.register("f", OBJECT_FINALIZE, boom)
    invs = fr.fire_object_finalize("b", "k")
    assert invs[0].ok is False
    assert "kaboom" in invs[0].error
    # one failure does not stop others
    fr.register("g", OBJECT_FINALIZE, lambda e: "ok")
    invs2 = fr.fire_object_finalize("b", "k")
    assert {i.ok for i in invs2} == {True, False}


def test_pubsub_publish_dispatch():
    fr = FunctionRunner()
    got = []
    fr.register("f", PUBSUB_PUBLISH, lambda e: got.append(e["data"]))
    fr.fire_pubsub_publish("topic", "hello")
    assert got == [b"hello"]


def test_wired_to_pubsub_auto_dispatch():
    ps = PubSub()
    fr = FunctionRunner(pubsub=ps)
    seen = []
    fr.register("f", PUBSUB_PUBLISH, lambda e: seen.append(e["data"]),
                resource="orders")
    ps.create_topic("orders")
    ps.publish("orders", b"new-order")
    assert seen == [b"new-order"]


def test_invocation_log_and_clear():
    fr = FunctionRunner()
    fr.register("f", OBJECT_FINALIZE, lambda e: 1)
    fr.fire_object_finalize("b", "k")
    fr.fire_object_finalize("b", "k2")
    assert len(fr.invocations()) == 2
    assert len(fr.invocations("f")) == 2
    fr.clear_log()
    assert fr.invocations() == []


def test_end_to_end_storage_finalize_via_runner():
    # storage write + explicit finalize dispatch (server normally wires this)
    s = ObjectStorage()
    fr = FunctionRunner(storage=s)
    log = []
    fr.register("indexer", OBJECT_FINALIZE,
                lambda e: log.append((e["bucket"], e["name"], e["size"])))
    s.create_bucket("uploads")
    meta = s.upload("uploads", "a.txt", b"12345")
    fr.fire_object_finalize("uploads", "a.txt", size=meta.size)
    assert log == [("uploads", "a.txt", 5)]
