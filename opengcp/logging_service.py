"""Cloud Logging-style structured log entry store.

Implements a compatible SUBSET of the Cloud Logging API:
  * Write log entries (any JSON-serialisable payload) with severity, labels,
    and a log name.
  * List / filter log entries by log name, severity, and a simple
    ``field=value`` expression (AND of equality predicates).
  * Delete a log by name.
  * ``tail`` — yield the N most-recent entries across all logs.

Severity levels (numeric, matches Cloud Logging):
  DEFAULT=0  DEBUG=100  INFO=200  NOTICE=300  WARNING=400
  ERROR=500  CRITICAL=600  ALERT=700  EMERGENCY=800

All data is stored in SQLite (in-memory by default).

This is an independent reimplementation for LOCAL development. It is NOT
affiliated with or endorsed by Google.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional


class LoggingError(Exception):
    pass


SEVERITY_MAP: Dict[str, int] = {
    "DEFAULT": 0,
    "DEBUG": 100,
    "INFO": 200,
    "NOTICE": 300,
    "WARNING": 400,
    "ERROR": 500,
    "CRITICAL": 600,
    "ALERT": 700,
    "EMERGENCY": 800,
}


def _severity_num(name_or_num) -> int:
    if isinstance(name_or_num, int):
        return name_or_num
    return SEVERITY_MAP.get(str(name_or_num).upper(), 0)


class LoggingService:
    """Thread-safe Cloud Logging emulator.

    ``path`` of None (default) uses an in-memory SQLite database.
    """

    def __init__(self, path: Optional[str] = None):
        self._lock = threading.RLock()
        db_path = path or ":memory:"
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS log_entries (
                    entry_id    TEXT PRIMARY KEY,
                    log_name    TEXT NOT NULL,
                    severity    INTEGER NOT NULL DEFAULT 0,
                    payload     TEXT NOT NULL,
                    labels      TEXT NOT NULL DEFAULT '{}',
                    resource    TEXT NOT NULL DEFAULT '{}',
                    timestamp   REAL NOT NULL,
                    insert_time REAL NOT NULL
                )
            """)
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_log_name ON log_entries (log_name)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_timestamp ON log_entries (timestamp)")
            self._conn.commit()

    # ---- write ----

    def write_entries(self, entries: List[Dict[str, Any]],
                      log_name: str = "") -> List[str]:
        """Write one or more log entries; return list of inserted entry IDs."""
        now = time.time()
        ids: List[str] = []
        with self._lock:
            for entry in entries:
                eid = uuid.uuid4().hex
                name = entry.get("logName", log_name) or "projects/local/logs/default"
                sev = _severity_num(entry.get("severity", 0))
                payload = entry.get("jsonPayload", entry.get("textPayload", {}))
                labels = entry.get("labels", {})
                resource = entry.get("resource", {})
                ts = entry.get("timestamp", now)
                if isinstance(ts, str):
                    # accept ISO-ish strings but just store float
                    try:
                        ts = float(ts)
                    except ValueError:
                        ts = now
                self._conn.execute(
                    "INSERT INTO log_entries VALUES (?,?,?,?,?,?,?,?)",
                    (eid, name, sev,
                     json.dumps(payload), json.dumps(labels),
                     json.dumps(resource), float(ts), now))
                ids.append(eid)
            self._conn.commit()
        return ids

    # ---- list / filter ----

    def list_entries(self, log_name: Optional[str] = None,
                     severity_min: Optional[str] = None,
                     filter_expr: Optional[str] = None,
                     order_by: str = "timestamp desc",
                     page_size: int = 1000) -> List[Dict[str, Any]]:
        """Return log entries, optionally filtered.

        ``filter_expr`` is a simple AND-of-equality expression:
            ``severity>=WARNING AND logName="projects/local/logs/app"``

        Supported predicates:
          ``logName = "..."``  ``logName != "..."``
          ``severity >= LEVEL``  ``severity > LEVEL`` etc.
          ``labels.key = "..."``
        """
        params: List[Any] = []
        clauses: List[str] = ["1=1"]

        if log_name:
            clauses.append("log_name = ?")
            params.append(log_name)

        if severity_min:
            clauses.append("severity >= ?")
            params.append(_severity_num(severity_min))

        if filter_expr:
            extra_clauses, extra_params = _parse_filter(filter_expr)
            clauses.extend(extra_clauses)
            params.extend(extra_params)

        direction = "DESC" if "desc" in order_by.lower() else "ASC"
        sql = (f"SELECT entry_id, log_name, severity, payload, labels, "
               f"resource, timestamp FROM log_entries "
               f"WHERE {' AND '.join(clauses)} "
               f"ORDER BY timestamp {direction}, insert_time {direction} "
               f"LIMIT ?")
        params.append(page_size)

        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]

    def tail(self, n: int = 100) -> List[Dict[str, Any]]:
        """Return the ``n`` most-recently-inserted log entries."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT entry_id, log_name, severity, payload, labels, "
                "resource, timestamp FROM log_entries "
                "ORDER BY insert_time DESC, timestamp DESC LIMIT ?",
                (n,)).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ---- delete ----

    def delete_log(self, log_name: str) -> int:
        """Delete all entries for a log; return the count deleted."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM log_entries WHERE log_name=?", (log_name,))
            self._conn.commit()
        return cur.rowcount

    def list_log_names(self) -> List[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT log_name FROM log_entries ORDER BY log_name"
            ).fetchall()
        return [r[0] for r in rows]


# ---- helpers ----

def _row_to_dict(row) -> Dict[str, Any]:
    return {
        "insertId": row[0],
        "logName": row[1],
        "severity": row[2],
        "jsonPayload": json.loads(row[3]),
        "labels": json.loads(row[4]),
        "resource": json.loads(row[5]),
        "timestamp": row[6],
    }


def _parse_filter(expr: str):
    """Very simple filter parser. Returns (sql_clauses, params) lists.

    Supported: ``logName = "x"`` ``severity >= WARNING`` ``labels.k = "v"``
    Joined by AND (case-insensitive).
    """
    import re
    clauses: List[str] = []
    params: List[Any] = []
    # split on AND keyword
    for part in re.split(r'\s+and\s+', expr.strip(), flags=re.IGNORECASE):
        part = part.strip()
        if not part:
            continue
        m = re.match(
            r'^([\w.]+)\s*(=|!=|>=|<=|>|<)\s*"?([^"]*)"?$', part)
        if not m:
            continue
        field, op, val = m.group(1), m.group(2), m.group(3)
        sql_op = {"=": "=", "!=": "!=", ">=": ">=", "<=": "<=",
                  ">": ">", "<": "<"}.get(op, "=")
        if field.lower() == "logname":
            clauses.append(f"log_name {sql_op} ?")
            params.append(val)
        elif field.lower() == "severity":
            clauses.append(f"severity {sql_op} ?")
            params.append(_severity_num(val))
        elif field.lower().startswith("labels."):
            # labels stored as JSON; use json_extract
            label_key = field[len("labels."):]
            clauses.append(f"json_extract(labels, '$.{label_key}') {sql_op} ?")
            params.append(val)
    return clauses, params
