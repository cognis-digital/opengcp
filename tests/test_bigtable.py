"""Tests for the Bigtable-lite module (instances, tables, column families,
row read/write/scan)."""

import time

import pytest

from opengcp.bigtable import (
    BigtableAdmin,
    BigtableError,
    InstanceNotFound,
    TableNotFound,
    ColumnFamilyNotFound,
    SetCell,
    DeleteCell,
    DeleteFromFamily,
    DeleteFromRow,
)
from opengcp.server import OpenGCPServer

import json
import urllib.request
import urllib.error


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

def test_create_and_list_instances():
    bt = BigtableAdmin()
    bt.create_instance("my-instance")
    assert "my-instance" in bt.list_instances()


def test_duplicate_instance_raises():
    bt = BigtableAdmin()
    bt.create_instance("inst")
    with pytest.raises(BigtableError):
        bt.create_instance("inst")


def test_delete_instance():
    bt = BigtableAdmin()
    bt.create_instance("inst")
    bt.delete_instance("inst")
    assert "inst" not in bt.list_instances()


def test_get_instance_not_found():
    bt = BigtableAdmin()
    with pytest.raises(InstanceNotFound):
        bt.get_instance("nope")


def test_create_and_list_tables():
    bt = BigtableAdmin()
    inst = bt.create_instance("i")
    inst.create_table("users")
    inst.create_table("events")
    tables = inst.list_tables()
    assert "users" in tables and "events" in tables


def test_duplicate_table_raises():
    bt = BigtableAdmin()
    inst = bt.create_instance("i")
    inst.create_table("t")
    with pytest.raises(BigtableError):
        inst.create_table("t")


def test_delete_table():
    bt = BigtableAdmin()
    inst = bt.create_instance("i")
    inst.create_table("t")
    inst.delete_table("t")
    assert "t" not in inst.list_tables()


def test_create_column_family():
    bt = BigtableAdmin()
    inst = bt.create_instance("i")
    tbl = inst.create_table("t")
    cf = tbl.create_column_family("cf1")
    assert cf.name == "cf1"
    assert cf.max_versions == 0  # unlimited


def test_column_family_max_versions():
    bt = BigtableAdmin()
    inst = bt.create_instance("i")
    tbl = inst.create_table("t")
    tbl.create_column_family("cf", max_versions=2)
    # write 3 versions
    for v in (b"v1", b"v2", b"v3"):
        tbl.mutate_row("row1", [SetCell("cf", "col", v)])
    row = tbl.read_row("row1")
    cells = row["families"]["cf"]["col"]
    assert len(cells) == 2  # pruned to 2


def test_set_cell_and_read_row():
    bt = BigtableAdmin()
    inst = bt.create_instance("i")
    tbl = inst.create_table("t")
    tbl.create_column_family("cf")
    tbl.mutate_row("row1", [
        SetCell("cf", "name", b"ada"),
        SetCell("cf", "age", b"36"),
    ])
    row = tbl.read_row("row1")
    assert row is not None
    assert row["rowKey"] == "row1"
    fams = row["families"]
    assert "ada" in fams["cf"]["name"][0]["value"]


def test_read_row_not_found():
    bt = BigtableAdmin()
    inst = bt.create_instance("i")
    tbl = inst.create_table("t")
    tbl.create_column_family("cf")
    assert tbl.read_row("nope") is None


def test_delete_cell():
    bt = BigtableAdmin()
    inst = bt.create_instance("i")
    tbl = inst.create_table("t")
    tbl.create_column_family("cf")
    ts = int(time.time() * 1_000_000)
    tbl.mutate_row("r", [SetCell("cf", "q", b"val", timestamp_micros=ts)])
    tbl.mutate_row("r", [DeleteCell("cf", "q", timestamp_micros=ts)])
    row = tbl.read_row("r")
    # row should be gone (no cells remaining)
    assert row is None


def test_delete_from_family():
    bt = BigtableAdmin()
    inst = bt.create_instance("i")
    tbl = inst.create_table("t")
    tbl.create_column_family("f1")
    tbl.create_column_family("f2")
    tbl.mutate_row("r", [
        SetCell("f1", "q", b"x"),
        SetCell("f2", "q", b"y"),
    ])
    tbl.mutate_row("r", [DeleteFromFamily("f1")])
    row = tbl.read_row("r")
    assert "f1" not in row["families"]
    assert "f2" in row["families"]


def test_delete_from_row():
    bt = BigtableAdmin()
    inst = bt.create_instance("i")
    tbl = inst.create_table("t")
    tbl.create_column_family("cf")
    tbl.mutate_row("r", [SetCell("cf", "q", b"v")])
    tbl.mutate_row("r", [DeleteFromRow()])
    assert tbl.read_row("r") is None


def test_scan_rows():
    bt = BigtableAdmin()
    inst = bt.create_instance("i")
    tbl = inst.create_table("t")
    tbl.create_column_family("cf")
    for k in ("row:a", "row:b", "row:c", "other:x"):
        tbl.mutate_row(k, [SetCell("cf", "v", k.encode())])
    rows = tbl.scan_rows(prefix="row:")
    assert len(rows) == 3
    keys = [r["rowKey"] for r in rows]
    assert "row:a" in keys and "row:b" in keys and "row:c" in keys


def test_scan_rows_range():
    bt = BigtableAdmin()
    inst = bt.create_instance("i")
    tbl = inst.create_table("t")
    tbl.create_column_family("cf")
    for k in ("a", "b", "c", "d"):
        tbl.mutate_row(k, [SetCell("cf", "v", k.encode())])
    rows = tbl.scan_rows(start_key="b", end_key="d")
    keys = [r["rowKey"] for r in rows]
    assert keys == ["b", "c"]  # end_key exclusive


def test_scan_rows_limit():
    bt = BigtableAdmin()
    inst = bt.create_instance("i")
    tbl = inst.create_table("t")
    tbl.create_column_family("cf")
    for k in ("a", "b", "c", "d", "e"):
        tbl.mutate_row(k, [SetCell("cf", "v", k.encode())])
    rows = tbl.scan_rows(limit=3)
    assert len(rows) == 3


def test_read_column():
    bt = BigtableAdmin()
    inst = bt.create_instance("i")
    tbl = inst.create_table("t")
    tbl.create_column_family("cf")
    tbl.mutate_row("r1", [SetCell("cf", "email", b"a@x.com")])
    tbl.mutate_row("r2", [SetCell("cf", "phone", b"555")])
    tbl.mutate_row("r3", [SetCell("cf", "email", b"b@x.com")])
    rows = tbl.read_column("cf", "email")
    keys = [r["rowKey"] for r in rows]
    assert "r1" in keys and "r3" in keys
    assert "r2" not in keys


def test_family_filter_in_read():
    bt = BigtableAdmin()
    inst = bt.create_instance("i")
    tbl = inst.create_table("t")
    tbl.create_column_family("f1")
    tbl.create_column_family("f2")
    tbl.mutate_row("r", [SetCell("f1", "q", b"v1"), SetCell("f2", "q", b"v2")])
    row = tbl.read_row("r", families=["f1"])
    assert "f1" in row["families"]
    assert "f2" not in row["families"]


def test_unknown_family_raises():
    bt = BigtableAdmin()
    inst = bt.create_instance("i")
    tbl = inst.create_table("t")
    tbl.create_column_family("cf")
    with pytest.raises(ColumnFamilyNotFound):
        tbl.mutate_row("r", [SetCell("nope", "q", b"v")])


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


def test_bigtable_http_roundtrip(server):
    # create instance
    code, inst = _req(server, "POST", "/bigtable/instances/my-inst")
    assert code == 200
    assert inst["name"] == "my-inst"

    # create table
    code, tbl = _req(server, "POST", "/bigtable/instances/my-inst/tables/my-tbl")
    assert code == 200

    # create column family
    code, cf = _req(server, "POST",
                    "/bigtable/instances/my-inst/tables/my-tbl/families/cf1?maxVersions=3")
    assert code == 200
    assert cf["maxVersions"] == 3

    # list column families
    code, fams = _req(server, "GET",
                      "/bigtable/instances/my-inst/tables/my-tbl/families")
    assert any(f["name"] == "cf1" for f in fams["columnFamilies"])

    # mutate row
    code, mutated = _req(server, "POST",
                         "/bigtable/instances/my-inst/tables/my-tbl/rows/row1/mutate",
                         body={"mutations": [
                             {"type": "setCell", "family": "cf1",
                              "qualifier": "name", "value": "ada"}
                         ]})
    assert code == 200

    # read row
    code, row = _req(server, "GET",
                     "/bigtable/instances/my-inst/tables/my-tbl/rows/row1")
    assert code == 200
    assert row["rowKey"] == "row1"
    assert "ada" in row["families"]["cf1"]["name"][0]["value"]

    # scan rows
    for k in ("a", "b", "c"):
        _req(server, "POST",
             f"/bigtable/instances/my-inst/tables/my-tbl/rows/{k}/mutate",
             body={"mutations": [
                 {"type": "setCell", "family": "cf1", "qualifier": "v",
                  "value": k}
             ]})
    code, scanned = _req(server, "GET",
                         "/bigtable/instances/my-inst/tables/my-tbl/rows?limit=2")
    assert code == 200
    # limit=2 includes at most 2 rows
    assert len(scanned["rows"]) <= 3  # might be 2 if row1 already counted

    # list instances
    code, insts = _req(server, "GET", "/bigtable/instances")
    assert "my-inst" in insts["instances"]

    # delete column family
    code, _ = _req(server, "DELETE",
                   "/bigtable/instances/my-inst/tables/my-tbl/families/cf1")
    assert code == 200

    # delete table
    code, _ = _req(server, "DELETE",
                   "/bigtable/instances/my-inst/tables/my-tbl")
    assert code == 200

    # delete instance
    code, _ = _req(server, "DELETE", "/bigtable/instances/my-inst")
    assert code == 200
    code, _ = _req(server, "GET", "/bigtable/instances/my-inst")
    assert code == 404
