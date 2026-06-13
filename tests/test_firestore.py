import pytest

from opengcp.firestore import DocumentStore, DocumentNotFound, FirestoreError


def test_create_and_get():
    db = DocumentStore()
    doc_id = db.create("users", {"name": "ada", "age": 36})
    assert db.get("users", doc_id) == {"name": "ada", "age": 36}


def test_create_with_explicit_id():
    db = DocumentStore()
    db.create("users", {"x": 1}, doc_id="u1")
    assert db.get("users", "u1") == {"x": 1}
    with pytest.raises(FirestoreError):
        db.create("users", {"x": 2}, doc_id="u1")


def test_get_missing_raises():
    db = DocumentStore()
    with pytest.raises(DocumentNotFound):
        db.get("users", "nope")


def test_set_create_or_replace():
    db = DocumentStore()
    db.set("c", "d", {"a": 1})
    assert db.get("c", "d") == {"a": 1}
    db.set("c", "d", {"b": 2})
    assert db.get("c", "d") == {"b": 2}


def test_update_merges():
    db = DocumentStore()
    db.set("c", "d", {"a": 1, "b": 2})
    doc = db.update("c", "d", {"b": 20, "c": 3})
    assert doc == {"a": 1, "b": 20, "c": 3}
    assert db.get("c", "d") == {"a": 1, "b": 20, "c": 3}


def test_update_missing_raises():
    db = DocumentStore()
    with pytest.raises(DocumentNotFound):
        db.update("c", "missing", {"x": 1})


def test_delete():
    db = DocumentStore()
    db.set("c", "d", {"x": 1})
    db.delete("c", "d")
    assert not db.exists("c", "d")
    with pytest.raises(DocumentNotFound):
        db.delete("c", "d")


def test_list_sorted():
    db = DocumentStore()
    db.set("c", "b", {})
    db.set("c", "a", {})
    ids = [i for i, _ in db.list("c")]
    assert ids == ["a", "b"]


def test_query_equality():
    db = DocumentStore()
    db.create("p", {"city": "NYC", "n": 1})
    db.create("p", {"city": "LA", "n": 2})
    db.create("p", {"city": "NYC", "n": 3})
    rows = db.query("p", "city", "==", "NYC")
    assert len(rows) == 2
    assert {d["n"] for _, d in rows} == {1, 3}


def test_query_comparisons():
    db = DocumentStore()
    for i in range(5):
        db.create("nums", {"v": i})
    assert len(db.query("nums", "v", ">", 2)) == 2
    assert len(db.query("nums", "v", ">=", 2)) == 3
    assert len(db.query("nums", "v", "<", 2)) == 2
    assert len(db.query("nums", "v", "!=", 0)) == 4


def test_query_limit_and_bad_op():
    db = DocumentStore()
    for i in range(10):
        db.create("nums", {"v": i})
    assert len(db.query("nums", "v", ">=", 0, limit=3)) == 3
    with pytest.raises(FirestoreError):
        db.query("nums", "v", "LIKE", 0)


def test_collections_listing():
    db = DocumentStore()
    db.set("a", "1", {})
    db.set("b", "1", {})
    assert db.collections() == ["a", "b"]


def test_non_object_rejected():
    db = DocumentStore()
    with pytest.raises(FirestoreError):
        db.create("c", [1, 2, 3])


def test_persistence(tmp_path):
    path = str(tmp_path / "fs.db")
    db = DocumentStore(path=path)
    db.set("c", "d", {"persisted": True})
    db.close()
    db2 = DocumentStore(path=path)
    assert db2.get("c", "d") == {"persisted": True}
