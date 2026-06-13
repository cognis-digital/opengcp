"""Tests for the Cloud Datastore-lite module (entities, kinds, keys, GQL queries)."""

import pytest

from opengcp.datastore import (
    DatastoreDB,
    Key,
    EntityNotFound,
    GQLSyntaxError,
    DatastoreError,
)
from opengcp.server import OpenGCPServer

import json
import urllib.request
import urllib.error


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

def test_put_and_get():
    ds = DatastoreDB()
    key = ds.put(Key("Person"), {"name": "ada", "age": 36})
    assert key.id is not None
    data = ds.get(key)
    assert data["name"] == "ada"
    assert data["age"] == 36


def test_put_with_explicit_int_id():
    ds = DatastoreDB()
    key = ds.put(Key("Item", 42), {"label": "x"})
    assert key.id == 42
    assert ds.get(Key("Item", 42))["label"] == "x"


def test_put_with_explicit_string_id():
    ds = DatastoreDB()
    ds.put(Key("Config", "global"), {"env": "prod"})
    assert ds.get(Key("Config", "global"))["env"] == "prod"


def test_put_upsert():
    ds = DatastoreDB()
    ds.put(Key("X", 1), {"v": 1})
    ds.put(Key("X", 1), {"v": 2})
    assert ds.get(Key("X", 1))["v"] == 2


def test_delete():
    ds = DatastoreDB()
    key = ds.put(Key("T"), {"a": 1})
    ds.delete(key)
    with pytest.raises(EntityNotFound):
        ds.get(key)


def test_delete_not_found():
    ds = DatastoreDB()
    with pytest.raises(EntityNotFound):
        ds.delete(Key("T", 999))


def test_list_kind():
    ds = DatastoreDB()
    ds.put(Key("Cat"), {"name": "a"})
    ds.put(Key("Cat"), {"name": "b"})
    rows = ds.list_kind("Cat")
    assert len(rows) == 2
    names = [d["name"] for _, d in rows]
    assert "a" in names and "b" in names


def test_kinds():
    ds = DatastoreDB()
    ds.put(Key("A"), {})
    ds.put(Key("B"), {})
    assert "A" in ds.kinds()
    assert "B" in ds.kinds()


def test_query_equality():
    ds = DatastoreDB()
    ds.put(Key("P"), {"name": "ada", "age": 36})
    ds.put(Key("P"), {"name": "grace", "age": 40})
    results = ds.query("P", conditions=[("name", "==", "ada")])
    assert len(results) == 1
    assert results[0][1]["name"] == "ada"


def test_query_comparison():
    ds = DatastoreDB()
    for i in range(5):
        ds.put(Key("N"), {"val": i})
    results = ds.query("N", conditions=[("val", ">=", 3)])
    assert all(d["val"] >= 3 for _, d in results)
    assert len(results) == 2


def test_query_order_and_limit():
    ds = DatastoreDB()
    for v in [5, 1, 3, 2, 4]:
        ds.put(Key("Num"), {"v": v})
    results = ds.query("Num", order_prop="v", order_desc=False, limit=3)
    assert [d["v"] for _, d in results] == [1, 2, 3]


def test_gql_select_all():
    ds = DatastoreDB()
    ds.put(Key("Z"), {"x": 1})
    ds.put(Key("Z"), {"x": 2})
    results = ds.gql("SELECT * FROM Z")
    assert len(results) == 2


def test_gql_where():
    ds = DatastoreDB()
    ds.put(Key("E"), {"score": 10})
    ds.put(Key("E"), {"score": 20})
    results = ds.gql("SELECT * FROM E WHERE score > 15")
    assert len(results) == 1
    assert results[0][1]["score"] == 20


def test_gql_where_and_order():
    ds = DatastoreDB()
    for name in ("carol", "alice", "bob"):
        ds.put(Key("User"), {"name": name, "active": True})
    ds.put(Key("User"), {"name": "dave", "active": False})
    results = ds.gql("SELECT * FROM User WHERE active = true ORDER BY name ASC")
    names = [d["name"] for _, d in results]
    assert names == sorted(names)
    assert "dave" not in names


def test_gql_limit():
    ds = DatastoreDB()
    for i in range(10):
        ds.put(Key("X"), {"i": i})
    results = ds.gql("SELECT * FROM X LIMIT 3")
    assert len(results) == 3


def test_gql_string_literal():
    ds = DatastoreDB()
    ds.put(Key("Dog"), {"breed": "labrador"})
    ds.put(Key("Dog"), {"breed": "poodle"})
    results = ds.gql("SELECT * FROM Dog WHERE breed = 'labrador'")
    assert len(results) == 1


def test_gql_syntax_error():
    ds = DatastoreDB()
    with pytest.raises(GQLSyntaxError):
        ds.gql("INSERT INTO X VALUES (1)")


def test_incomplete_key_get_raises():
    ds = DatastoreDB()
    with pytest.raises(DatastoreError):
        ds.get(Key("T"))


# ---------------------------------------------------------------------------
# HTTP round-trip
# ---------------------------------------------------------------------------

@pytest.fixture()
def server():
    srv = OpenGCPServer(host="127.0.0.1", port=0).start_background()
    yield srv
    srv.stop()


def _req(srv, method, path, body=None):
    url = srv.base_url + path
    data = None
    hdrs = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_datastore_http_roundtrip(server):
    # create entity
    code, body = _req(server, "POST", "/datastore/Person",
                      body={"name": "ada", "age": 36})
    assert code == 200
    key_id = body["key"]["id"]

    # get entity
    code, got = _req(server, "GET", f"/datastore/Person/{key_id}")
    assert got["data"]["name"] == "ada"

    # put (upsert) entity with explicit id
    code, _ = _req(server, "PUT", "/datastore/Person/named_key",
                   body={"name": "grace", "age": 40})
    assert code == 200

    # list kind
    code, listed = _req(server, "GET", "/datastore/Person")
    assert len(listed["entities"]) == 2

    # GQL query via ?gql=
    import urllib.parse
    gql = urllib.parse.quote("SELECT * FROM Person WHERE age > 37")
    code, qres = _req(server, "GET", f"/datastore/Person?gql={gql}")
    assert len(qres["entities"]) == 1
    assert qres["entities"][0]["data"]["name"] == "grace"

    # delete
    code, _ = _req(server, "DELETE", f"/datastore/Person/{key_id}")
    assert code == 200
    code, _ = _req(server, "GET", f"/datastore/Person/{key_id}")
    assert code == 404

    # list kinds
    code, kinds = _req(server, "GET", "/datastore/kinds")
    assert "Person" in kinds["kinds"]
