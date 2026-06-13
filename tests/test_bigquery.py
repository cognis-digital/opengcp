"""Tests for the BigQuery-lite module (datasets, tables, insertAll, query engine)."""

import pytest

from opengcp.bigquery import (
    BigQueryDB,
    BigQueryError,
    DatasetNotFound,
    TableNotFound,
    QuerySyntaxError,
)
from opengcp.server import OpenGCPServer

import json
import urllib.request
import urllib.error


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

def test_create_and_get_dataset():
    bq = BigQueryDB()
    info = bq.create_dataset("my_ds")
    assert info["datasetReference"]["datasetId"] == "my_ds"
    got = bq.get_dataset("my_ds")
    assert got["datasetReference"]["datasetId"] == "my_ds"


def test_duplicate_dataset_raises():
    bq = BigQueryDB()
    bq.create_dataset("ds")
    with pytest.raises(BigQueryError):
        bq.create_dataset("ds")


def test_list_datasets():
    bq = BigQueryDB()
    bq.create_dataset("a")
    bq.create_dataset("b")
    ids = [d["datasetId"] for d in bq.list_datasets()]
    assert "a" in ids and "b" in ids


def test_delete_dataset_empty():
    bq = BigQueryDB()
    bq.create_dataset("ds")
    bq.delete_dataset("ds")
    with pytest.raises(DatasetNotFound):
        bq.get_dataset("ds")


def test_delete_dataset_nonempty_raises():
    bq = BigQueryDB()
    bq.create_dataset("ds")
    bq.create_table("ds", "t", [])
    with pytest.raises(BigQueryError, match="not empty"):
        bq.delete_dataset("ds")


def test_delete_dataset_with_contents():
    bq = BigQueryDB()
    bq.create_dataset("ds")
    bq.create_table("ds", "t", [])
    bq.delete_dataset("ds", delete_contents=True)
    with pytest.raises(DatasetNotFound):
        bq.get_dataset("ds")


def test_create_and_get_table():
    bq = BigQueryDB()
    bq.create_dataset("ds")
    schema = [{"name": "id", "type": "INTEGER"}, {"name": "name", "type": "STRING"}]
    tbl = bq.create_table("ds", "users", schema)
    assert tbl.table_id == "users"
    got = bq.get_table("ds", "users")
    assert got.table_id == "users"


def test_duplicate_table_raises():
    bq = BigQueryDB()
    bq.create_dataset("ds")
    bq.create_table("ds", "t", [])
    with pytest.raises(BigQueryError):
        bq.create_table("ds", "t", [])


def test_list_tables():
    bq = BigQueryDB()
    bq.create_dataset("ds")
    bq.create_table("ds", "a", [])
    bq.create_table("ds", "b", [])
    ids = [t["tableId"] for t in bq.list_tables("ds")]
    assert "a" in ids and "b" in ids


def test_delete_table():
    bq = BigQueryDB()
    bq.create_dataset("ds")
    bq.create_table("ds", "t", [])
    bq.delete_table("ds", "t")
    with pytest.raises(TableNotFound):
        bq.get_table("ds", "t")


def test_insert_all_and_scan():
    bq = BigQueryDB()
    bq.create_dataset("ds")
    bq.create_table("ds", "events", [{"name": "ts", "type": "INTEGER"},
                                      {"name": "msg", "type": "STRING"}])
    rows = [
        {"insertId": "r1", "json": {"ts": 1, "msg": "hello"}},
        {"insertId": "r2", "json": {"ts": 2, "msg": "world"}},
    ]
    result = bq.insert_all("ds", "events", rows)
    assert result["insertErrors"] == []
    tbl = bq.get_table("ds", "events")
    assert len(tbl.scan()) == 2


def test_query_select_all():
    bq = BigQueryDB()
    bq.create_dataset("d")
    bq.create_table("d", "t", [])
    bq.insert_all("d", "t", [
        {"json": {"x": 1}},
        {"json": {"x": 2}},
    ])
    rows = bq.query("SELECT * FROM d.t")
    assert len(rows) == 2


def test_query_select_columns():
    bq = BigQueryDB()
    bq.create_dataset("d")
    bq.create_table("d", "t", [])
    bq.insert_all("d", "t", [{"json": {"a": 1, "b": 2, "c": 3}}])
    rows = bq.query("SELECT a, b FROM d.t")
    assert rows[0] == {"a": 1, "b": 2}
    assert "c" not in rows[0]


def test_query_where_equality():
    bq = BigQueryDB()
    bq.create_dataset("d")
    bq.create_table("d", "t", [])
    bq.insert_all("d", "t", [
        {"json": {"name": "ada", "age": 36}},
        {"json": {"name": "grace", "age": 40}},
    ])
    rows = bq.query("SELECT * FROM d.t WHERE name = 'ada'")
    assert len(rows) == 1
    assert rows[0]["name"] == "ada"


def test_query_where_comparison():
    bq = BigQueryDB()
    bq.create_dataset("d")
    bq.create_table("d", "t", [])
    for i in range(5):
        bq.insert_all("d", "t", [{"json": {"val": i}}])
    rows = bq.query("SELECT * FROM d.t WHERE val >= 3")
    assert all(r["val"] >= 3 for r in rows)
    assert len(rows) == 2


def test_query_where_like():
    bq = BigQueryDB()
    bq.create_dataset("d")
    bq.create_table("d", "t", [])
    bq.insert_all("d", "t", [
        {"json": {"email": "alice@example.com"}},
        {"json": {"email": "bob@other.com"}},
        {"json": {"email": "carol@example.com"}},
    ])
    rows = bq.query("SELECT * FROM d.t WHERE email LIKE '%example%'")
    assert len(rows) == 2


def test_query_where_and():
    bq = BigQueryDB()
    bq.create_dataset("d")
    bq.create_table("d", "t", [])
    bq.insert_all("d", "t", [
        {"json": {"x": 1, "y": 2}},
        {"json": {"x": 1, "y": 5}},
        {"json": {"x": 2, "y": 2}},
    ])
    rows = bq.query("SELECT * FROM d.t WHERE x = 1 AND y > 3")
    assert len(rows) == 1
    assert rows[0]["y"] == 5


def test_query_order_by():
    bq = BigQueryDB()
    bq.create_dataset("d")
    bq.create_table("d", "t", [])
    for v in [3, 1, 2]:
        bq.insert_all("d", "t", [{"json": {"v": v}}])
    rows = bq.query("SELECT * FROM d.t ORDER BY v ASC")
    vals = [r["v"] for r in rows]
    assert vals == sorted(vals)


def test_query_order_by_desc():
    bq = BigQueryDB()
    bq.create_dataset("d")
    bq.create_table("d", "t", [])
    for v in [3, 1, 2]:
        bq.insert_all("d", "t", [{"json": {"v": v}}])
    rows = bq.query("SELECT * FROM d.t ORDER BY v DESC")
    vals = [r["v"] for r in rows]
    assert vals == sorted(vals, reverse=True)


def test_query_limit():
    bq = BigQueryDB()
    bq.create_dataset("d")
    bq.create_table("d", "t", [])
    for i in range(10):
        bq.insert_all("d", "t", [{"json": {"i": i}}])
    rows = bq.query("SELECT * FROM d.t LIMIT 3")
    assert len(rows) == 3


def test_query_count_star():
    bq = BigQueryDB()
    bq.create_dataset("d")
    bq.create_table("d", "t", [])
    for i in range(7):
        bq.insert_all("d", "t", [{"json": {"i": i}}])
    rows = bq.query("SELECT COUNT(*) FROM d.t")
    assert rows[0]["COUNT_*"] == 7


def test_query_sum():
    bq = BigQueryDB()
    bq.create_dataset("d")
    bq.create_table("d", "t", [])
    bq.insert_all("d", "t", [
        {"json": {"v": 10}},
        {"json": {"v": 20}},
        {"json": {"v": 30}},
    ])
    rows = bq.query("SELECT SUM(v) FROM d.t")
    assert rows[0]["SUM_v"] == 60.0


def test_query_avg():
    bq = BigQueryDB()
    bq.create_dataset("d")
    bq.create_table("d", "t", [])
    bq.insert_all("d", "t", [
        {"json": {"v": 10}},
        {"json": {"v": 20}},
    ])
    rows = bq.query("SELECT AVG(v) FROM d.t")
    assert rows[0]["AVG_v"] == 15.0


def test_query_min_max():
    bq = BigQueryDB()
    bq.create_dataset("d")
    bq.create_table("d", "t", [])
    bq.insert_all("d", "t", [
        {"json": {"v": 5}},
        {"json": {"v": 1}},
        {"json": {"v": 9}},
    ])
    rows = bq.query("SELECT MIN(v), MAX(v) FROM d.t")
    assert rows[0]["MIN_v"] == 1
    assert rows[0]["MAX_v"] == 9


def test_query_group_by():
    bq = BigQueryDB()
    bq.create_dataset("d")
    bq.create_table("d", "t", [])
    bq.insert_all("d", "t", [
        {"json": {"dept": "eng", "salary": 100}},
        {"json": {"dept": "eng", "salary": 200}},
        {"json": {"dept": "hr",  "salary": 80}},
    ])
    rows = bq.query("SELECT dept, COUNT(*) FROM d.t GROUP BY dept ORDER BY dept ASC")
    dept_map = {r["dept"]: r["COUNT_*"] for r in rows}
    assert dept_map["eng"] == 2
    assert dept_map["hr"] == 1


def test_query_group_by_sum():
    bq = BigQueryDB()
    bq.create_dataset("d")
    bq.create_table("d", "t", [])
    bq.insert_all("d", "t", [
        {"json": {"cat": "a", "v": 10}},
        {"json": {"cat": "a", "v": 5}},
        {"json": {"cat": "b", "v": 20}},
    ])
    rows = bq.query("SELECT cat, SUM(v) FROM d.t GROUP BY cat ORDER BY cat ASC")
    cat_map = {r["cat"]: r["SUM_v"] for r in rows}
    assert cat_map["a"] == 15.0
    assert cat_map["b"] == 20.0


def test_query_syntax_error():
    bq = BigQueryDB()
    with pytest.raises(QuerySyntaxError):
        bq.query("BADSTMT")


def test_query_table_not_found():
    bq = BigQueryDB()
    bq.create_dataset("d")
    with pytest.raises(QuerySyntaxError):
        bq.query("SELECT * FROM d.no_such_table")


def test_persistence_across_instances(tmp_path):
    path = str(tmp_path / "bq.db")
    bq = BigQueryDB(path=path)
    bq.create_dataset("ds")
    bq.create_table("ds", "t", [{"name": "x", "type": "INTEGER"}])
    bq.insert_all("ds", "t", [{"json": {"x": 42}}])
    # reload
    bq2 = BigQueryDB(path=path)
    rows = bq2.query("SELECT * FROM ds.t")
    assert rows[0]["x"] == 42


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


def test_bigquery_http_roundtrip(server):
    # create dataset
    code, ds = _req(server, "POST", "/bigquery/datasets/myds")
    assert code == 200
    assert ds["datasetReference"]["datasetId"] == "myds"

    # create table
    schema = [{"name": "id", "type": "INTEGER"}, {"name": "name", "type": "STRING"}]
    code, tbl = _req(server, "POST", "/bigquery/datasets/myds/tables/users",
                     body={"schema": schema})
    assert code == 200

    # insertAll
    rows = [
        {"insertId": "r1", "json": {"id": 1, "name": "ada"}},
        {"insertId": "r2", "json": {"id": 2, "name": "grace"}},
    ]
    code, ins = _req(server, "POST",
                     "/bigquery/datasets/myds/tables/users/insertAll",
                     body={"rows": rows})
    assert code == 200
    assert ins["insertErrors"] == []

    # query
    code, qres = _req(server, "POST", "/bigquery/query",
                      body={"query": "SELECT * FROM myds.users WHERE id = 1"})
    assert code == 200
    assert len(qres["rows"]) == 1
    assert qres["rows"][0]["name"] == "ada"

    # aggregation query
    code, agg = _req(server, "POST", "/bigquery/query",
                     body={"query": "SELECT COUNT(*) FROM myds.users"})
    assert code == 200
    assert agg["rows"][0]["COUNT_*"] == 2

    # list datasets
    code, datasets = _req(server, "GET", "/bigquery/datasets")
    assert any(d["datasetId"] == "myds" for d in datasets["datasets"])

    # list tables
    code, tables = _req(server, "GET", "/bigquery/datasets/myds/tables")
    assert any(t["tableId"] == "users" for t in tables["tables"])

    # get table
    code, tbl_info = _req(server, "GET", "/bigquery/datasets/myds/tables/users")
    assert tbl_info["tableReference"]["tableId"] == "users"

    # delete table
    code, _ = _req(server, "DELETE", "/bigquery/datasets/myds/tables/users")
    assert code == 200
    code, _ = _req(server, "GET", "/bigquery/datasets/myds/tables/users")
    assert code == 404

    # delete dataset
    code, _ = _req(server, "DELETE", "/bigquery/datasets/myds")
    assert code == 200
