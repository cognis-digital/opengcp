"""Tests for the extended Cloud Storage features added in the storage+data pass:
  - Custom object metadata
  - copy_object
  - compose
  - list_objects with delimiter
  - Object versioning (enable, soft-delete, list versions, delete_version)
  - Bucket lifecycle stub
  - update_metadata
  - HTTP round-trips for the new operations via the running server
"""

import pytest

from opengcp.storage import (
    ObjectStorage,
    BucketNotFound,
    BucketAlreadyExists,
    ObjectNotFound,
)
from opengcp.server import OpenGCPServer


# ---------------------------------------------------------------------------
# Unit tests — ObjectStorage
# ---------------------------------------------------------------------------

def test_custom_metadata_round_trip():
    s = ObjectStorage()
    s.create_bucket("b")
    meta = s.upload("b", "k", b"v", metadata={"author": "ada", "version": "1"})
    assert meta.custom_metadata == {"author": "ada", "version": "1"}
    d = meta.to_dict()
    assert d["metadata"] == {"author": "ada", "version": "1"}
    # stat returns the same metadata
    assert s.stat("b", "k").custom_metadata == {"author": "ada", "version": "1"}


def test_update_metadata():
    s = ObjectStorage()
    s.create_bucket("b")
    s.upload("b", "k", b"v", metadata={"a": "1"})
    m = s.update_metadata("b", "k", {"b": "2"})
    assert m.custom_metadata == {"b": "2"}
    assert s.stat("b", "k").custom_metadata == {"b": "2"}


def test_update_metadata_not_found():
    s = ObjectStorage()
    s.create_bucket("b")
    with pytest.raises(ObjectNotFound):
        s.update_metadata("b", "nope", {})


def test_copy_object_same_bucket():
    s = ObjectStorage()
    s.create_bucket("b")
    s.upload("b", "src", b"hello-copy", content_type="text/plain")
    meta = s.copy_object("b", "src", "b", "dst")
    assert meta.name == "dst"
    assert s.download("b", "dst") == b"hello-copy"
    assert s.stat("b", "dst").content_type == "text/plain"


def test_copy_object_cross_bucket():
    s = ObjectStorage()
    s.create_bucket("src-bkt")
    s.create_bucket("dst-bkt")
    s.upload("src-bkt", "file", b"data", content_type="application/json")
    meta = s.copy_object("src-bkt", "file", "dst-bkt", "copy")
    assert s.download("dst-bkt", "copy") == b"data"
    assert s.stat("dst-bkt", "copy").content_type == "application/json"


def test_copy_object_override_metadata():
    s = ObjectStorage()
    s.create_bucket("b")
    s.upload("b", "src", b"x", metadata={"original": "yes"})
    s.copy_object("b", "src", "b", "dst", metadata={"original": "no"})
    assert s.stat("b", "dst").custom_metadata == {"original": "no"}


def test_compose():
    s = ObjectStorage()
    s.create_bucket("b")
    s.upload("b", "part1", b"Hello, ")
    s.upload("b", "part2", b"world")
    s.upload("b", "part3", b"!")
    meta = s.compose("b", "result", ["part1", "part2", "part3"])
    assert s.download("b", "result") == b"Hello, world!"
    assert meta.size == len(b"Hello, world!")


def test_compose_missing_source():
    s = ObjectStorage()
    s.create_bucket("b")
    s.upload("b", "p1", b"x")
    with pytest.raises(ObjectNotFound):
        s.compose("b", "out", ["p1", "nope"])


def test_compose_too_many_sources():
    s = ObjectStorage()
    s.create_bucket("b")
    for i in range(33):
        s.upload("b", f"p{i}", b"x")
    from opengcp.storage import StorageError
    with pytest.raises(StorageError, match="32"):
        s.compose("b", "out", [f"p{i}" for i in range(33)])


def test_list_objects_delimiter():
    s = ObjectStorage()
    s.create_bucket("b")
    for n in ("a/x", "a/y", "a/z/deep", "b/1"):
        s.upload("b", n, b"v")
    items, prefixes = s.list_objects("b", prefix="", delimiter="/")
    assert prefixes == ["a/", "b/"]
    assert items == []  # all names contain the delimiter after prefix


def test_list_objects_delimiter_with_prefix():
    s = ObjectStorage()
    s.create_bucket("b")
    for n in ("a/x", "a/y", "a/sub/z"):
        s.upload("b", n, b"v")
    items, prefixes = s.list_objects("b", prefix="a/", delimiter="/")
    names = [m.name for m in items]
    assert "a/x" in names
    assert "a/y" in names
    assert "a/sub/" in prefixes
    assert "a/sub/z" not in names


def test_versioning_enable():
    s = ObjectStorage()
    info = s.create_bucket("b", versioning_enabled=True)
    assert info["versioning"]["enabled"] is True
    s.set_versioning("b", False)
    assert s.get_bucket("b")["versioning"]["enabled"] is False


def test_versioning_soft_delete():
    s = ObjectStorage()
    s.create_bucket("b", versioning_enabled=True)
    m1 = s.upload("b", "k", b"v1")
    assert m1.generation == 1
    m2 = s.upload("b", "k", b"v2")
    assert m2.generation == 2
    # live object is v2
    assert s.download("b", "k") == b"v2"
    # versioned download
    assert s.download("b", "k", generation=1) == b"v1"
    # soft delete moves live to noncurrent
    s.delete("b", "k")
    with pytest.raises(ObjectNotFound):
        s.download("b", "k")
    # noncurrent still accessible by generation
    assert s.download("b", "k", generation=2) == b"v2"


def test_versioning_list_versions():
    s = ObjectStorage()
    s.create_bucket("b", versioning_enabled=True)
    s.upload("b", "k", b"v1")
    s.upload("b", "k", b"v2")
    items, _ = s.list_objects("b", versions=True)
    # live (gen2) + noncurrent (gen1)
    assert len(items) == 2
    gens = sorted(m.generation for m in items)
    assert gens == [1, 2]


def test_versioning_delete_specific_version():
    s = ObjectStorage()
    s.create_bucket("b", versioning_enabled=True)
    s.upload("b", "k", b"v1")
    s.upload("b", "k", b"v2")
    s.delete_version("b", "k", 1)
    items, _ = s.list_objects("b", versions=True)
    assert len(items) == 1
    assert items[0].generation == 2


def test_lifecycle_stub():
    s = ObjectStorage()
    s.create_bucket("b")
    rules = [{"action": {"type": "Delete"}, "condition": {"age": 30}}]
    info = s.set_lifecycle("b", rules)
    assert info["lifecycle"]["rules"] == rules
    assert s.get_bucket("b")["lifecycle"]["rules"] == rules


def test_persistence_with_versioning(tmp_path):
    root = str(tmp_path / "store")
    s = ObjectStorage(root=root)
    s.create_bucket("b", versioning_enabled=True)
    s.upload("b", "k", b"v1")
    s.upload("b", "k", b"v2")
    # reload
    s2 = ObjectStorage(root=root)
    assert s2.download("b", "k") == b"v2"
    assert s2.download("b", "k", generation=1) == b"v1"


# ---------------------------------------------------------------------------
# HTTP round-trip tests
# ---------------------------------------------------------------------------

import json
import urllib.request
import urllib.error


@pytest.fixture()
def server():
    srv = OpenGCPServer(host="127.0.0.1", port=0).start_background()
    yield srv
    srv.stop()


def _req(srv, method, path, body=None, headers=None, raw=False):
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
        payload = e.read()
        if raw:
            return e.code, payload
        return e.code, json.loads(payload)


def test_storage_extended_http(server):
    _req(server, "POST", "/storage/b/bkt")
    # upload with custom metadata header
    code, meta = _req(server, "POST", "/storage/b/bkt/o/hello.txt",
                      body=b"hello",
                      headers={"Content-Type": "text/plain",
                               "X-Goog-Meta-author": "ada"})
    assert code == 200
    assert meta["metadata"]["author"] == "ada"

    # PATCH to update metadata
    code, patched = _req(server, "PATCH", "/storage/b/bkt/o/hello.txt",
                         body={"author": "linus"})
    assert code == 200
    assert patched["metadata"]["author"] == "linus"

    # list with delimiter
    _req(server, "POST", "/storage/b/bkt/o/dir/a.txt", body=b"x")
    _req(server, "POST", "/storage/b/bkt/o/dir/b.txt", body=b"x")
    code, lst = _req(server, "GET", "/storage/b/bkt/o?delimiter=%2F")
    assert "dir/" in lst["prefixes"]

    # copy
    code, copied = _req(server, "POST",
                        "/storage/b/bkt/o/hello.txt/copy?dst=bkt%2Fcopy.txt")
    assert code == 200
    assert copied["name"] == "copy.txt"
    code, data = _req(server, "GET", "/storage/b/bkt/o/copy.txt", raw=True)
    assert data == b"hello"

    # compose
    _req(server, "POST", "/storage/b/bkt/o/p1", body=b"foo")
    _req(server, "POST", "/storage/b/bkt/o/p2", body=b"bar")
    code, comp = _req(server, "POST",
                      "/storage/b/bkt/o/composed/compose",
                      body={"sources": ["p1", "p2"],
                            "contentType": "text/plain"})
    assert code == 200
    assert comp["size"] == 6
    code, cdata = _req(server, "GET", "/storage/b/bkt/o/composed", raw=True)
    assert cdata == b"foobar"


def test_storage_versioning_http(server):
    # create versioned bucket
    code, bkt = _req(server, "POST", "/storage/b/vbkt?versioning=1")
    assert code == 200
    assert bkt["versioning"]["enabled"] is True

    # upload v1
    _req(server, "POST", "/storage/b/vbkt/o/k", body=b"v1")
    # upload v2
    _req(server, "POST", "/storage/b/vbkt/o/k", body=b"v2")
    # download live (v2)
    code, data = _req(server, "GET", "/storage/b/vbkt/o/k", raw=True)
    assert data == b"v2"
    # download v1 by generation
    code, data = _req(server, "GET", "/storage/b/vbkt/o/k?generation=1", raw=True)
    assert data == b"v1"
    # list with versions
    code, lst = _req(server, "GET", "/storage/b/vbkt/o?versions=1")
    assert len(lst["items"]) == 2

    # soft delete
    _req(server, "DELETE", "/storage/b/vbkt/o/k")
    code, _ = _req(server, "GET", "/storage/b/vbkt/o/k", raw=True)
    assert code == 404
    code, data = _req(server, "GET", "/storage/b/vbkt/o/k?generation=2", raw=True)
    assert data == b"v2"

    # delete specific generation
    code, _ = _req(server, "DELETE", "/storage/b/vbkt/o/k?generation=2")
    assert code == 200


def test_storage_lifecycle_http(server):
    _req(server, "POST", "/storage/b/lbkt")
    rules = [{"action": {"type": "Delete"}, "condition": {"age": 365}}]
    code, info = _req(server, "PATCH", "/storage/b/lbkt/lifecycle",
                      body={"rules": rules})
    assert code == 200
    assert info["lifecycle"]["rules"] == rules
