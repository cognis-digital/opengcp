"""Firestore-style document database backed by SQLite (or in-memory SQLite).

Implements a compatible SUBSET of the Firestore document model:
collections that hold documents, each document a JSON object addressed by id.
Supports create/get/set/update/delete, listing a collection, and simple
field-equality / comparison queries.

This is an independent reimplementation for LOCAL development. It is NOT
affiliated with or endorsed by Google.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple


class FirestoreError(Exception):
    pass


class DocumentNotFound(FirestoreError):
    pass


_ALLOWED_OPS = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    "<": lambda a, b: a is not None and a < b,
    "<=": lambda a, b: a is not None and a <= b,
    ">": lambda a, b: a is not None and a > b,
    ">=": lambda a, b: a is not None and a >= b,
}


class DocumentStore:
    """Thread-safe document store.

    ``path`` of None (default) uses an in-memory SQLite database. A filesystem
    path persists the data across restarts.
    """

    def __init__(self, path: Optional[str] = None):
        self._lock = threading.RLock()
        self._path = path or ":memory:"
        # check_same_thread=False + our own RLock makes this safe across the
        # HTTP server's worker threads.
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    collection TEXT NOT NULL,
                    doc_id     TEXT NOT NULL,
                    data       TEXT NOT NULL,
                    created    REAL NOT NULL,
                    updated    REAL NOT NULL,
                    PRIMARY KEY (collection, doc_id)
                )
                """
            )
            self._conn.commit()

    # ----- write operations -----
    def create(self, collection: str, data: Dict[str, Any],
               doc_id: Optional[str] = None) -> str:
        if not isinstance(data, dict):
            raise FirestoreError("document data must be an object")
        doc_id = doc_id or uuid.uuid4().hex
        now = time.time()
        with self._lock:
            existing = self._conn.execute(
                "SELECT 1 FROM documents WHERE collection=? AND doc_id=?",
                (collection, doc_id)).fetchone()
            if existing:
                raise FirestoreError(f"document already exists: {collection}/{doc_id}")
            self._conn.execute(
                "INSERT INTO documents VALUES (?,?,?,?,?)",
                (collection, doc_id, json.dumps(data), now, now))
            self._conn.commit()
        return doc_id

    def set(self, collection: str, doc_id: str, data: Dict[str, Any]) -> None:
        """Create-or-replace the whole document."""
        if not isinstance(data, dict):
            raise FirestoreError("document data must be an object")
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT created FROM documents WHERE collection=? AND doc_id=?",
                (collection, doc_id)).fetchone()
            created = row[0] if row else now
            self._conn.execute(
                "INSERT OR REPLACE INTO documents VALUES (?,?,?,?,?)",
                (collection, doc_id, json.dumps(data), created, now))
            self._conn.commit()

    def update(self, collection: str, doc_id: str,
               fields: Dict[str, Any]) -> Dict[str, Any]:
        """Merge ``fields`` into an existing document."""
        with self._lock:
            doc = self.get(collection, doc_id)
            doc.update(fields)
            now = time.time()
            self._conn.execute(
                "UPDATE documents SET data=?, updated=? WHERE collection=? AND doc_id=?",
                (json.dumps(doc), now, collection, doc_id))
            self._conn.commit()
            return doc

    def delete(self, collection: str, doc_id: str) -> None:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM documents WHERE collection=? AND doc_id=?",
                (collection, doc_id))
            self._conn.commit()
            if cur.rowcount == 0:
                raise DocumentNotFound(f"{collection}/{doc_id}")

    # ----- read operations -----
    def get(self, collection: str, doc_id: str) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM documents WHERE collection=? AND doc_id=?",
                (collection, doc_id)).fetchone()
        if row is None:
            raise DocumentNotFound(f"{collection}/{doc_id}")
        return json.loads(row[0])

    def exists(self, collection: str, doc_id: str) -> bool:
        try:
            self.get(collection, doc_id)
            return True
        except DocumentNotFound:
            return False

    def list(self, collection: str) -> List[Tuple[str, Dict[str, Any]]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT doc_id, data FROM documents WHERE collection=? ORDER BY doc_id",
                (collection,)).fetchall()
        return [(r[0], json.loads(r[1])) for r in rows]

    def query(self, collection: str, field: str, op: str, value: Any,
              limit: Optional[int] = None) -> List[Tuple[str, Dict[str, Any]]]:
        """Return ``(doc_id, doc)`` pairs where ``doc[field] op value`` holds.

        Comparison is performed in Python so it honours JSON types. Documents
        missing the field are excluded for ordered comparisons and treated as
        not-equal for ``==``.
        """
        if op not in _ALLOWED_OPS:
            raise FirestoreError(f"unsupported operator: {op}")
        pred = _ALLOWED_OPS[op]
        out: List[Tuple[str, Dict[str, Any]]] = []
        for doc_id, doc in self.list(collection):
            if field not in doc:
                if op == "!=":
                    out.append((doc_id, doc))
                continue
            try:
                if pred(doc[field], value):
                    out.append((doc_id, doc))
            except TypeError:
                # incomparable types -> skip
                continue
            if limit is not None and len(out) >= limit:
                break
        return out

    def collections(self) -> List[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT collection FROM documents ORDER BY collection"
            ).fetchall()
        return [r[0] for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
