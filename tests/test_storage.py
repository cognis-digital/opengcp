import base64
import hashlib

import pytest

from opengcp.storage import (ObjectStorage, BucketNotFound, BucketAlreadyExists,
                             ObjectNotFound)


def test_create_and_get_bucket():
    s = ObjectStorage()
    info = s.create_bucket("photos")
    assert info["name"] == "photos"
    assert s.get_bucket("photos")["name"] == "photos"
    assert [b["name"] for b in s.list_buckets()] == ["photos"]


def test_duplicate_bucket_raises():
    s = ObjectStorage()
    s.create_bucket("b")
    with pytest.raises(BucketAlreadyExists):
        s.create_bucket("b")


def test_missing_bucket_raises():
    s = ObjectStorage()
    with pytest.raises(BucketNotFound):
        s.get_bucket("nope")
    with pytest.raises(BucketNotFound):
        s.upload("nope", "x", b"y")


def test_upload_download_roundtrip():
    s = ObjectStorage()
    s.create_bucket("b")
    payload = b"hello world" * 100
    meta = s.upload("b", "dir/file.txt", payload, content_type="text/plain")
    assert meta.size == len(payload)
    assert meta.content_type == "text/plain"
    assert meta.generation == 1
    expected_md5 = base64.b64encode(hashlib.md5(payload).digest()).decode()
    assert meta.md5_hash == expected_md5
    assert s.download("b", "dir/file.txt") == payload


def test_overwrite_bumps_generation():
    s = ObjectStorage()
    s.create_bucket("b")
    m1 = s.upload("b", "k", b"v1")
    m2 = s.upload("b", "k", b"v2")
    assert m1.generation == 1
    assert m2.generation == 2
    assert s.download("b", "k") == b"v2"


def test_list_objects_prefix():
    s = ObjectStorage()
    s.create_bucket("b")
    s.upload("b", "a/1", b"x")
    s.upload("b", "a/2", b"x")
    s.upload("b", "b/1", b"x")
    items, _ = s.list_objects("b", prefix="a/")
    names = [m.name for m in items]
    assert names == ["a/1", "a/2"]
    all_items, _ = s.list_objects("b")
    assert len(all_items) == 3


def test_delete_object():
    s = ObjectStorage()
    s.create_bucket("b")
    s.upload("b", "k", b"v")
    s.delete("b", "k")
    with pytest.raises(ObjectNotFound):
        s.download("b", "k")
    with pytest.raises(ObjectNotFound):
        s.delete("b", "k")


def test_delete_bucket_removes_objects():
    s = ObjectStorage()
    s.create_bucket("b")
    s.upload("b", "k", b"v")
    s.delete_bucket("b")
    assert s.list_buckets() == []
    with pytest.raises(BucketNotFound):
        s.list_objects("b")


def test_string_payload_is_encoded():
    s = ObjectStorage()
    s.create_bucket("b")
    s.upload("b", "k", "unicode ✓")
    assert s.download("b", "k").decode("utf-8") == "unicode ✓"


def test_persistence_across_instances(tmp_path):
    root = str(tmp_path / "store")
    s = ObjectStorage(root=root)
    s.create_bucket("b")
    s.upload("b", "deep/key/name", b"persisted", content_type="text/x")
    # new instance reads from disk
    s2 = ObjectStorage(root=root)
    assert [b["name"] for b in s2.list_buckets()] == ["b"]
    assert s2.download("b", "deep/key/name") == b"persisted"
    assert s2.stat("b", "deep/key/name").content_type == "text/x"


def test_name_with_path_traversal_is_safe(tmp_path):
    root = str(tmp_path / "store")
    s = ObjectStorage(root=root)
    s.create_bucket("b")
    s.upload("b", "../../etc/passwd", b"safe")
    # round trips by key, does not escape
    assert s.download("b", "../../etc/passwd") == b"safe"
    s2 = ObjectStorage(root=root)
    assert s2.download("b", "../../etc/passwd") == b"safe"
