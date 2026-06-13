"""BigQuery-lite: datasets, tables with schema, insertAll, SELECT/WHERE/aggregation.

Implements a compatible SUBSET of the BigQuery data model:

  - **Datasets** are top-level namespaces.
  - **Tables** have a declared schema (list of ``{name, type}`` field defs; type
    is advisory — storage is JSON, so any serialisable value is accepted).
  - **insertAll** (streaming inserts): append one or more rows as a list of
    ``{insertId, json}`` dicts, mirroring the BigQuery tabledata.insertAll API.
    Duplicate insertId detection within the same table (optional, controlled by
    ``skipInvalidRows`` / ``ignoreUnknownValues`` flags that we store but do not
    enforce strictly).
  - **query**: a real SQL-lite SELECT executor operating over the in-memory
    rows, supporting:
      - ``SELECT *`` or ``SELECT col1, col2, ...``
      - ``FROM dataset.table``
      - ``WHERE col op literal [AND col op literal ...]``
        ops: ``=``, ``!=``, ``<``, ``<=``, ``>``, ``>=``, ``LIKE``
        (LIKE supports ``%`` wildcard at start and/or end)
      - ``GROUP BY col`` with aggregates ``COUNT(*)``, ``SUM(col)``,
        ``AVG(col)``, ``MIN(col)``, ``MAX(col)`` in the SELECT list.
      - ``ORDER BY col [ASC|DESC]``
      - ``LIMIT n``
  - Storage backend: SQLite (or in-memory SQLite) — rows stored as JSON blobs,
    enabling persistence across process restarts via ``--data-dir``.

This is an independent reimplementation for LOCAL development. It is NOT
affiliated with or endorsed by Google.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, Iterator, List, Optional, Tuple


class BigQueryError(Exception):
    """Base class for BigQuery errors."""


class DatasetNotFound(BigQueryError):
    pass


class TableNotFound(BigQueryError):
    pass


class QuerySyntaxError(BigQueryError):
    pass


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

def _coerce(value: Any, type_hint: str) -> Any:
    """Best-effort type coercion for display; storage is always JSON."""
    t = type_hint.upper()
    if value is None:
        return None
    try:
        if t in ("INTEGER", "INT64"):
            return int(value)
        if t in ("FLOAT", "FLOAT64", "NUMERIC"):
            return float(value)
        if t == "BOOLEAN":
            if isinstance(value, bool):
                return value
            return str(value).lower() in ("true", "1")
        if t == "STRING":
            return str(value)
    except (ValueError, TypeError):
        pass
    return value


# ---------------------------------------------------------------------------
# SQL-lite parser
# ---------------------------------------------------------------------------

# Tokeniser ‑ very small hand-rolled approach to avoid importlib overhead
_IDENT_RE = re.compile(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?')
_INT_RE = re.compile(r'-?\d+(?:\.\d+)?')
_STR_RE = re.compile(r"'[^']*'|\"[^\"]*\"")

# Aggregate functions recognised in SELECT
_AGG_RE = re.compile(
    r'(?P<func>COUNT|SUM|AVG|MIN|MAX)\s*\(\s*(?P<arg>\*|\w+)\s*\)',
    re.IGNORECASE,
)


def _parse_select(sql: str) -> dict:
    """Parse a limited SQL SELECT and return a query descriptor dict.

    Returns::

        {
          "columns": ["*"] | [str, ...],           # projected column names
          "aggregates": [{"func": str, "arg": str, "alias": str}, ...],
          "dataset": str,
          "table": str,
          "conditions": [(prop, op, value), ...],
          "group_by": str | None,
          "order_prop": str | None,
          "order_desc": bool,
          "limit": int | None,
        }
    """
    # Normalise whitespace
    s = re.sub(r'\s+', ' ', sql.strip())

    # ----- SELECT clause -----
    m = re.match(r'SELECT\s+(.+?)\s+FROM\s+', s, re.IGNORECASE)
    if not m:
        raise QuerySyntaxError(f"expected SELECT ... FROM ...: {sql!r}")
    select_raw = m.group(1).strip()
    rest = s[m.end():].strip()

    columns: List[str] = []
    aggregates: List[dict] = []
    if select_raw.strip() == "*":
        columns = ["*"]
    else:
        for tok in select_raw.split(","):
            tok = tok.strip()
            am = _AGG_RE.fullmatch(tok)
            if am:
                alias = f"{am.group('func').upper()}_{am.group('arg')}"
                aggregates.append({
                    "func": am.group("func").upper(),
                    "arg": am.group("arg"),
                    "alias": alias,
                })
            else:
                columns.append(tok)

    # ----- FROM clause -----
    from_m = re.match(r'([\w.]+)\s*', rest, re.IGNORECASE)
    if not from_m:
        raise QuerySyntaxError(f"missing table reference in: {sql!r}")
    table_ref = from_m.group(1)
    if "." in table_ref:
        dataset, table = table_ref.split(".", 1)
    else:
        raise QuerySyntaxError(f"table reference must be dataset.table, got: {table_ref!r}")
    rest = rest[from_m.end():].strip()

    # ----- WHERE clause -----
    conditions: List[Tuple[str, str, Any]] = []
    where_m = re.match(r'WHERE\s+(.+?)(?:\s+GROUP BY|\s+ORDER BY|\s+LIMIT|$)',
                       rest, re.IGNORECASE)
    if where_m:
        where_str = where_m.group(1).strip()
        rest = rest[where_m.end():].strip()
        for part in re.split(r'\s+AND\s+', where_str, flags=re.IGNORECASE):
            part = part.strip()
            cond_m = re.match(
                r'(\w+)\s*(=|!=|<=|>=|<|>|LIKE)\s*(.+)',
                part, re.IGNORECASE,
            )
            if not cond_m:
                raise QuerySyntaxError(f"invalid WHERE condition: {part!r}")
            prop = cond_m.group(1)
            op = cond_m.group(2).upper()
            if op == "=":
                op = "=="
            val_raw = cond_m.group(3).strip()
            try:
                val = json.loads(val_raw)
            except json.JSONDecodeError:
                # bare string
                val = val_raw.strip("'\"")
            conditions.append((prop, op, val))

    # ----- GROUP BY -----
    group_by: Optional[str] = None
    gb_m = re.match(r'GROUP BY\s+(\w+)', rest, re.IGNORECASE)
    if gb_m:
        group_by = gb_m.group(1)
        rest = rest[gb_m.end():].strip()

    # ----- ORDER BY -----
    order_prop: Optional[str] = None
    order_desc = False
    ob_m = re.match(r'ORDER BY\s+(\w+)(?:\s+(ASC|DESC))?', rest, re.IGNORECASE)
    if ob_m:
        order_prop = ob_m.group(1)
        order_desc = (ob_m.group(2) or "ASC").upper() == "DESC"
        rest = rest[ob_m.end():].strip()

    # ----- LIMIT -----
    limit: Optional[int] = None
    lim_m = re.match(r'LIMIT\s+(\d+)', rest, re.IGNORECASE)
    if lim_m:
        limit = int(lim_m.group(1))

    return {
        "columns": columns,
        "aggregates": aggregates,
        "dataset": dataset,
        "table": table,
        "conditions": conditions,
        "group_by": group_by,
        "order_prop": order_prop,
        "order_desc": order_desc,
        "limit": limit,
    }


def _like_match(value: str, pattern: str) -> bool:
    # Build regex: escape everything, then convert LIKE wildcards
    # We must handle % and _ before re.escape since % is not a regex special char
    # but _ is.  Split on wildcards, escape each part, then rejoin.
    parts = re.split(r'(%|_)', pattern)
    regex_parts = []
    for part in parts:
        if part == '%':
            regex_parts.append('.*')
        elif part == '_':
            regex_parts.append('.')
        else:
            regex_parts.append(re.escape(part))
    regex = ''.join(regex_parts)
    return bool(re.fullmatch(regex, str(value), re.IGNORECASE))


def _apply_cond(row: dict, prop: str, op: str, val: Any) -> bool:
    rv = row.get(prop)
    try:
        if op == "==":
            return rv == val
        if op == "!=":
            return rv != val
        if op == "<":
            return rv is not None and rv < val
        if op == "<=":
            return rv is not None and rv <= val
        if op == ">":
            return rv is not None and rv > val
        if op == ">=":
            return rv is not None and rv >= val
        if op == "LIKE":
            return _like_match(rv, val)
    except TypeError:
        return False
    return False


# ---------------------------------------------------------------------------
# BQTable  (backed by a SQLite table of JSON row blobs)
# ---------------------------------------------------------------------------

class BQTable:
    """A single BigQuery-style table."""

    def __init__(self, dataset: str, table_id: str,
                 schema: List[Dict[str, str]],
                 conn: sqlite3.Connection,
                 lock: threading.RLock):
        self.dataset = dataset
        self.table_id = table_id
        self.schema = list(schema)
        self._conn = conn
        self._lock = lock
        sql_name = self._sql_name(dataset, table_id)
        with self._lock:
            self._conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS "{sql_name}" (
                    insert_id  TEXT,
                    row_data   TEXT NOT NULL,
                    inserted   REAL NOT NULL
                )
                """
            )
            self._conn.commit()

    @staticmethod
    def _sql_name(dataset: str, table_id: str) -> str:
        return f"bq__{dataset}__{table_id}"

    def insert_all(self, rows: List[Dict[str, Any]],
                   skip_invalid: bool = False,
                   ignore_unknown: bool = True) -> dict:
        """Insert rows (streaming insert style).

        Each element of ``rows`` should be ``{"insertId": str, "json": dict}``.
        Returns ``{"insertErrors": []}`` on success (mirrors the API).
        """
        sql_name = self._sql_name(self.dataset, self.table_id)
        errors = []
        now = time.time()
        with self._lock:
            for i, row_wrapper in enumerate(rows):
                insert_id = row_wrapper.get("insertId") or uuid.uuid4().hex
                data = row_wrapper.get("json", row_wrapper)
                if not isinstance(data, dict):
                    if skip_invalid:
                        errors.append({"index": i, "errors": [{"reason": "invalid"}]})
                        continue
                    raise BigQueryError(f"row {i}: 'json' must be a dict")
                self._conn.execute(
                    f'INSERT INTO "{sql_name}" VALUES (?, ?, ?)',
                    (insert_id, json.dumps(data), now),
                )
            self._conn.commit()
        return {"insertErrors": errors}

    def _iter_rows(self) -> Iterator[dict]:
        sql_name = self._sql_name(self.dataset, self.table_id)
        with self._lock:
            rows = self._conn.execute(
                f'SELECT row_data FROM "{sql_name}" ORDER BY rowid'
            ).fetchall()
        for (row_json,) in rows:
            yield json.loads(row_json)

    def scan(self) -> List[dict]:
        return list(self._iter_rows())

    def to_dict(self) -> dict:
        return {
            "kind": "bigquery#table",
            "tableReference": {
                "datasetId": self.dataset,
                "tableId": self.table_id,
            },
            "schema": {"fields": self.schema},
            "numRows": sum(1 for _ in self._iter_rows()),
        }


# ---------------------------------------------------------------------------
# BigQueryDB
# ---------------------------------------------------------------------------

class BigQueryDB:
    """Thread-safe BigQuery-lite engine.

    ``path`` of None (default) uses an in-memory SQLite database.
    """

    def __init__(self, path: Optional[str] = None):
        self._lock = threading.RLock()
        self._path = path or ":memory:"
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        # cache of BQTable objects — must be initialised before _init_schema()
        # because _init_schema() may reload tables from a persistent DB.
        self._tables: Dict[str, BQTable] = {}
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bq_datasets (
                    dataset_id TEXT PRIMARY KEY,
                    created    REAL NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bq_tables (
                    dataset_id TEXT NOT NULL,
                    table_id   TEXT NOT NULL,
                    schema_def TEXT NOT NULL,
                    created    REAL NOT NULL,
                    PRIMARY KEY (dataset_id, table_id)
                )
                """
            )
            self._conn.commit()
        # reload table objects from persistent metadata
        with self._lock:
            rows = self._conn.execute(
                "SELECT dataset_id, table_id, schema_def FROM bq_tables"
            ).fetchall()
        for dataset_id, table_id, schema_json in rows:
            key = f"{dataset_id}.{table_id}"
            self._tables[key] = BQTable(
                dataset_id, table_id,
                json.loads(schema_json),
                self._conn,
                self._lock,
            )

    # ----- dataset operations -----

    def create_dataset(self, dataset_id: str) -> dict:
        now = time.time()
        with self._lock:
            existing = self._conn.execute(
                "SELECT 1 FROM bq_datasets WHERE dataset_id=?", (dataset_id,)
            ).fetchone()
            if existing:
                raise BigQueryError(f"dataset already exists: {dataset_id}")
            self._conn.execute(
                "INSERT INTO bq_datasets VALUES (?, ?)", (dataset_id, now)
            )
            self._conn.commit()
        return {"kind": "bigquery#dataset",
                "datasetReference": {"datasetId": dataset_id},
                "creationTime": now}

    def get_dataset(self, dataset_id: str) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT created FROM bq_datasets WHERE dataset_id=?",
                (dataset_id,),
            ).fetchone()
        if row is None:
            raise DatasetNotFound(dataset_id)
        return {"kind": "bigquery#dataset",
                "datasetReference": {"datasetId": dataset_id},
                "creationTime": row[0]}

    def list_datasets(self) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT dataset_id, created FROM bq_datasets ORDER BY dataset_id"
            ).fetchall()
        return [{"datasetId": r[0], "creationTime": r[1]} for r in rows]

    def delete_dataset(self, dataset_id: str, delete_contents: bool = False) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM bq_datasets WHERE dataset_id=?", (dataset_id,)
            ).fetchone()
            if row is None:
                raise DatasetNotFound(dataset_id)
            tables = self._conn.execute(
                "SELECT table_id FROM bq_tables WHERE dataset_id=?", (dataset_id,)
            ).fetchall()
            if tables and not delete_contents:
                raise BigQueryError(
                    f"dataset {dataset_id!r} is not empty; use delete_contents=True"
                )
            for (tid,) in tables:
                self._drop_table(dataset_id, tid)
            self._conn.execute(
                "DELETE FROM bq_datasets WHERE dataset_id=?", (dataset_id,)
            )
            self._conn.commit()

    # ----- table operations -----

    def create_table(self, dataset_id: str, table_id: str,
                     schema: List[Dict[str, str]]) -> BQTable:
        with self._lock:
            if self._conn.execute(
                "SELECT 1 FROM bq_datasets WHERE dataset_id=?", (dataset_id,)
            ).fetchone() is None:
                raise DatasetNotFound(dataset_id)
            if self._conn.execute(
                "SELECT 1 FROM bq_tables WHERE dataset_id=? AND table_id=?",
                (dataset_id, table_id),
            ).fetchone():
                raise BigQueryError(f"table {dataset_id}.{table_id} already exists")
            now = time.time()
            self._conn.execute(
                "INSERT INTO bq_tables VALUES (?, ?, ?, ?)",
                (dataset_id, table_id, json.dumps(schema), now),
            )
            self._conn.commit()
        key = f"{dataset_id}.{table_id}"
        tbl = BQTable(dataset_id, table_id, schema, self._conn, self._lock)
        with self._lock:
            self._tables[key] = tbl
        return tbl

    def get_table(self, dataset_id: str, table_id: str) -> BQTable:
        key = f"{dataset_id}.{table_id}"
        with self._lock:
            if key not in self._tables:
                raise TableNotFound(f"{dataset_id}.{table_id}")
            return self._tables[key]

    def list_tables(self, dataset_id: str) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT table_id FROM bq_tables WHERE dataset_id=? ORDER BY table_id",
                (dataset_id,),
            ).fetchall()
        return [{"tableId": r[0], "datasetId": dataset_id} for r in rows]

    def _drop_table(self, dataset_id: str, table_id: str) -> None:
        """Internal — caller must hold self._lock."""
        key = f"{dataset_id}.{table_id}"
        self._tables.pop(key, None)
        sql_name = BQTable._sql_name(dataset_id, table_id)
        self._conn.execute(f'DROP TABLE IF EXISTS "{sql_name}"')
        self._conn.execute(
            "DELETE FROM bq_tables WHERE dataset_id=? AND table_id=?",
            (dataset_id, table_id),
        )
        self._conn.commit()

    def delete_table(self, dataset_id: str, table_id: str) -> None:
        with self._lock:
            if self._conn.execute(
                "SELECT 1 FROM bq_tables WHERE dataset_id=? AND table_id=?",
                (dataset_id, table_id),
            ).fetchone() is None:
                raise TableNotFound(f"{dataset_id}.{table_id}")
            self._drop_table(dataset_id, table_id)

    # ----- data operations -----

    def insert_all(self, dataset_id: str, table_id: str,
                   rows: List[Dict[str, Any]],
                   skip_invalid: bool = False,
                   ignore_unknown: bool = True) -> dict:
        tbl = self.get_table(dataset_id, table_id)
        return tbl.insert_all(rows, skip_invalid=skip_invalid,
                              ignore_unknown=ignore_unknown)

    # ----- query engine -----

    def query(self, sql: str) -> List[dict]:
        """Execute a SQL-lite SELECT and return a list of result rows (dicts)."""
        parsed = _parse_select(sql)
        try:
            tbl = self.get_table(parsed["dataset"], parsed["table"])
        except (DatasetNotFound, TableNotFound) as exc:
            raise QuerySyntaxError(str(exc)) from exc

        # 1. Scan + filter
        rows = [r for r in tbl.scan() if all(
            _apply_cond(r, prop, op, val)
            for prop, op, val in parsed["conditions"]
        )]

        # 2. GROUP BY + aggregation
        if parsed["aggregates"]:
            rows = self._aggregate(rows, parsed["aggregates"], parsed["group_by"])
        else:
            # 2b. Project columns (non-aggregate SELECT)
            if parsed["columns"] != ["*"]:
                rows = [{c: r.get(c) for c in parsed["columns"]} for r in rows]

        # 3. ORDER BY
        if parsed["order_prop"]:
            op = parsed["order_prop"]
            rows.sort(key=lambda r: (r.get(op) is None, r.get(op)),
                      reverse=parsed["order_desc"])

        # 4. LIMIT
        if parsed["limit"] is not None:
            rows = rows[:parsed["limit"]]

        return rows

    @staticmethod
    def _aggregate(rows: List[dict],
                   aggregates: List[dict],
                   group_by: Optional[str]) -> List[dict]:
        """Compute aggregate functions, optionally grouped."""
        from collections import defaultdict

        def _group_key(row: dict) -> Any:
            return row.get(group_by) if group_by else "__ALL__"

        groups: Dict[Any, List[dict]] = defaultdict(list)
        for row in rows:
            groups[_group_key(row)].append(row)

        results = []
        for group_val, group_rows in groups.items():
            out: dict = {}
            if group_by:
                out[group_by] = group_val
            for agg in aggregates:
                func = agg["func"]
                arg = agg["arg"]
                alias = agg["alias"]
                if func == "COUNT":
                    out[alias] = len(group_rows)
                elif func == "SUM":
                    vals = [r.get(arg) for r in group_rows if r.get(arg) is not None]
                    out[alias] = sum(float(v) for v in vals) if vals else 0
                elif func == "AVG":
                    vals = [r.get(arg) for r in group_rows if r.get(arg) is not None]
                    out[alias] = (sum(float(v) for v in vals) / len(vals)) if vals else None
                elif func == "MIN":
                    vals = [r.get(arg) for r in group_rows if r.get(arg) is not None]
                    out[alias] = min(vals) if vals else None
                elif func == "MAX":
                    vals = [r.get(arg) for r in group_rows if r.get(arg) is not None]
                    out[alias] = max(vals) if vals else None
            results.append(out)
        return results

    def close(self) -> None:
        with self._lock:
            self._conn.close()
