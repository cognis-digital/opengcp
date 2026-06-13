"""Cloud Datastore-style entity store backed by SQLite (or in-memory SQLite).

Implements a compatible SUBSET of the Cloud Datastore / Datastore (legacy)
entity model:

  - Entities belong to a *kind* (analogous to a table).
  - Each entity has a *key*: a (kind, id) pair where id is either an integer
    (auto-assigned when you call ``put`` without one) or a string name.
  - Entity properties are arbitrary JSON-serialisable values stored as a single
    JSON blob (we do not split them into typed columns, matching how Datastore
    treats flexible schemas).
  - GQL-lite queries: ``SELECT * FROM <kind> [WHERE <prop> <op> <literal>
    [AND <prop> <op> <literal>]*] [ORDER BY <prop> [ASC|DESC]] [LIMIT <n>]``
    Supported ops: ``=``, ``!=``, ``<``, ``<=``, ``>``, ``>=``.
  - Ancestor queries are not supported (no entity groups).

This is an independent reimplementation for LOCAL development. It is NOT
affiliated with or endorsed by Google.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple, Union


class DatastoreError(Exception):
    """Base class for datastore errors."""


class EntityNotFound(DatastoreError):
    pass


class GQLSyntaxError(DatastoreError):
    pass


# ---------------------------------------------------------------------------
# Key representation
# ---------------------------------------------------------------------------

class Key:
    """Represents a Datastore entity key: (kind, id_or_name)."""

    __slots__ = ("kind", "id")

    def __init__(self, kind: str, id_or_name: Union[int, str, None] = None):
        if not kind:
            raise DatastoreError("kind must be a non-empty string")
        self.kind = kind
        self.id: Union[int, str, None] = id_or_name

    def complete(self) -> bool:
        return self.id is not None

    def to_dict(self) -> dict:
        return {"kind": self.kind, "id": self.id}

    def __repr__(self) -> str:
        return f"Key({self.kind!r}, {self.id!r})"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Key):
            return NotImplemented
        return self.kind == other.kind and self.id == other.id

    def __hash__(self) -> int:
        return hash((self.kind, self.id))


# ---------------------------------------------------------------------------
# GQL-lite parser
# ---------------------------------------------------------------------------

_GQL_RE = re.compile(
    r"""
    SELECT\s+\*\s+FROM\s+(?P<kind>\w+)
    (?:\s+WHERE\s+(?P<where>.+?))?
    (?:\s+ORDER\s+BY\s+(?P<order_prop>\w+)(?:\s+(?P<order_dir>ASC|DESC))?)?
    (?:\s+LIMIT\s+(?P<limit>\d+))?
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

_COND_RE = re.compile(
    r"""(?P<prop>\w+)\s*(?P<op>=|!=|<=|>=|<|>)\s*(?P<val>.+)""",
    re.IGNORECASE,
)

_ALLOWED_OPS = {"=", "!=", "<", "<=", ">", ">="}


def _parse_literal(s: str) -> Any:
    s = s.strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # single-quoted string literal
    if len(s) >= 2 and s[0] == "'" and s[-1] == "'":
        return s[1:-1]
    # bare strings without quotes
    return s


def _parse_gql(gql: str) -> dict:
    """Return a dict with keys: kind, conditions, order_prop, order_desc, limit."""
    m = _GQL_RE.match(gql.strip())
    if not m:
        raise GQLSyntaxError(f"invalid GQL: {gql!r}")
    conditions: List[Tuple[str, str, Any]] = []
    if m.group("where"):
        for part in re.split(r"\s+AND\s+", m.group("where"), flags=re.IGNORECASE):
            cm = _COND_RE.match(part.strip())
            if not cm:
                raise GQLSyntaxError(f"invalid WHERE clause: {part!r}")
            op = cm.group("op")
            if op == "=":
                op = "=="   # normalise to Python equality
            if op not in {"==", "!=", "<", "<=", ">", ">="}:
                raise GQLSyntaxError(f"unsupported operator: {op!r}")
            conditions.append((cm.group("prop"), op, _parse_literal(cm.group("val"))))
    return {
        "kind": m.group("kind"),
        "conditions": conditions,
        "order_prop": m.group("order_prop"),
        "order_desc": (m.group("order_dir") or "ASC").upper() == "DESC",
        "limit": int(m.group("limit")) if m.group("limit") else None,
    }


def _apply_op(a: Any, op: str, b: Any) -> bool:
    try:
        if op == "==":
            return a == b
        if op == "!=":
            return a != b
        if op == "<":
            return a < b
        if op == "<=":
            return a <= b
        if op == ">":
            return a > b
        if op == ">=":
            return a >= b
    except TypeError:
        return False
    return False


# ---------------------------------------------------------------------------
# DatastoreDB
# ---------------------------------------------------------------------------

class DatastoreDB:
    """Thread-safe entity store.

    ``path`` of None (default) uses an in-memory SQLite database.
    """

    def __init__(self, path: Optional[str] = None):
        self._lock = threading.RLock()
        self._path = path or ":memory:"
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()
        self._auto_id: Dict[str, int] = {}

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS entities (
                    kind        TEXT NOT NULL,
                    entity_id   TEXT NOT NULL,
                    id_type     TEXT NOT NULL,   -- 'int' or 'str'
                    data        TEXT NOT NULL,
                    created     REAL NOT NULL,
                    updated     REAL NOT NULL,
                    PRIMARY KEY (kind, entity_id)
                )
                """
            )
            self._conn.commit()

    # ----- write operations -----

    def put(self, key: Key, data: Dict[str, Any]) -> Key:
        """Upsert an entity.  If key.id is None a new integer id is assigned."""
        if not isinstance(data, dict):
            raise DatastoreError("entity data must be a dict")
        now = time.time()
        with self._lock:
            if key.id is None:
                # auto-assign integer id
                existing_max = self._conn.execute(
                    "SELECT MAX(CAST(entity_id AS INTEGER)) FROM entities WHERE kind=? AND id_type='int'",
                    (key.kind,),
                ).fetchone()[0]
                key = Key(key.kind, (existing_max or 0) + 1)
            id_type = "int" if isinstance(key.id, int) else "str"
            entity_id = str(key.id)
            row = self._conn.execute(
                "SELECT created FROM entities WHERE kind=? AND entity_id=?",
                (key.kind, entity_id),
            ).fetchone()
            created = row[0] if row else now
            self._conn.execute(
                "INSERT OR REPLACE INTO entities VALUES (?,?,?,?,?,?)",
                (key.kind, entity_id, id_type, json.dumps(data), created, now),
            )
            self._conn.commit()
        return key

    def get(self, key: Key) -> Dict[str, Any]:
        """Return entity data for key; raises EntityNotFound if absent."""
        if not key.complete():
            raise DatastoreError("key must have an id to get")
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM entities WHERE kind=? AND entity_id=?",
                (key.kind, str(key.id)),
            ).fetchone()
        if row is None:
            raise EntityNotFound(f"{key.kind}/{key.id}")
        return json.loads(row[0])

    def delete(self, key: Key) -> None:
        if not key.complete():
            raise DatastoreError("key must have an id to delete")
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM entities WHERE kind=? AND entity_id=?",
                (key.kind, str(key.id)),
            )
            self._conn.commit()
            if cur.rowcount == 0:
                raise EntityNotFound(f"{key.kind}/{key.id}")

    # ----- read operations -----

    def list_kind(self, kind: str) -> List[Tuple[Key, Dict[str, Any]]]:
        """Return all entities of the given kind, ordered by entity_id."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT entity_id, id_type, data FROM entities WHERE kind=? ORDER BY entity_id",
                (kind,),
            ).fetchall()
        result = []
        for entity_id, id_type, data_json in rows:
            eid: Union[int, str] = int(entity_id) if id_type == "int" else entity_id
            result.append((Key(kind, eid), json.loads(data_json)))
        return result

    def kinds(self) -> List[str]:
        """Return all distinct kinds in the store."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT kind FROM entities ORDER BY kind"
            ).fetchall()
        return [r[0] for r in rows]

    def query(self, kind: str,
              conditions: Optional[List[Tuple[str, str, Any]]] = None,
              order_prop: Optional[str] = None,
              order_desc: bool = False,
              limit: Optional[int] = None) -> List[Tuple[Key, Dict[str, Any]]]:
        """Programmatic query — same as gql() but with parsed arguments."""
        rows = self.list_kind(kind)
        results: List[Tuple[Key, Dict[str, Any]]] = []
        for key, data in rows:
            if conditions:
                ok = True
                for prop, op, val in conditions:
                    entity_val = data.get(prop)
                    if not _apply_op(entity_val, op, val):
                        ok = False
                        break
                if not ok:
                    continue
            results.append((key, data))
        if order_prop:
            results.sort(
                key=lambda kv: (kv[1].get(order_prop) is None,
                                kv[1].get(order_prop)),
                reverse=order_desc,
            )
        if limit is not None:
            results = results[:limit]
        return results

    def gql(self, gql_str: str) -> List[Tuple[Key, Dict[str, Any]]]:
        """Execute a GQL-lite query string.

        Supports::

            SELECT * FROM Kind
            SELECT * FROM Kind WHERE prop = value [AND ...]
            SELECT * FROM Kind WHERE prop = value ORDER BY prop [ASC|DESC] LIMIT n
        """
        parsed = _parse_gql(gql_str)
        return self.query(
            kind=parsed["kind"],
            conditions=parsed["conditions"],
            order_prop=parsed["order_prop"],
            order_desc=parsed["order_desc"],
            limit=parsed["limit"],
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
