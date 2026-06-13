"""Cloud Secret Manager-style secret store.

Implements a compatible SUBSET of the Secret Manager API:
  * Create / get / list / delete secrets (with optional metadata labels).
  * Add secret versions (binary payload, stored as-is).
  * Access (read) a specific version or the "latest" alias.
  * Enable / disable / destroy individual versions.
  * Version state machine: ENABLED → DISABLED → DESTROYED.

All data is stored in SQLite (in-memory by default).

This is an independent reimplementation for LOCAL development. It is NOT
affiliated with or endorsed by Google.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional


class SecretManagerError(Exception):
    pass


class SecretNotFound(SecretManagerError):
    pass


class VersionNotFound(SecretManagerError):
    pass


class VersionStateError(SecretManagerError):
    pass


class SecretManager:
    """Thread-safe Secret Manager emulator.

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
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS secrets (
                    secret_id   TEXT PRIMARY KEY,
                    labels      TEXT NOT NULL DEFAULT '{}',
                    created     REAL NOT NULL,
                    updated     REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS secret_versions (
                    secret_id   TEXT NOT NULL,
                    version_num INTEGER NOT NULL,
                    payload     BLOB NOT NULL,
                    state       TEXT NOT NULL DEFAULT 'ENABLED',
                    created     REAL NOT NULL,
                    PRIMARY KEY (secret_id, version_num)
                );
            """)
            self._conn.commit()

    # ---- secrets ----

    def create_secret(self, secret_id: str,
                      labels: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            if self._conn.execute(
                    "SELECT 1 FROM secrets WHERE secret_id=?",
                    (secret_id,)).fetchone():
                raise SecretManagerError(f"secret already exists: {secret_id}")
            self._conn.execute(
                "INSERT INTO secrets VALUES (?,?,?,?)",
                (secret_id, json.dumps(labels or {}), now, now))
            self._conn.commit()
        return self._secret_dict(secret_id, labels or {}, now)

    def get_secret(self, secret_id: str) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT secret_id, labels, created FROM secrets WHERE secret_id=?",
                (secret_id,)).fetchone()
        if row is None:
            raise SecretNotFound(secret_id)
        return self._secret_dict(row[0], json.loads(row[1]), row[2])

    def list_secrets(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT secret_id, labels, created FROM secrets "
                "ORDER BY secret_id").fetchall()
        return [self._secret_dict(r[0], json.loads(r[1]), r[2]) for r in rows]

    def delete_secret(self, secret_id: str) -> None:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM secrets WHERE secret_id=?", (secret_id,))
            self._conn.execute(
                "DELETE FROM secret_versions WHERE secret_id=?", (secret_id,))
            self._conn.commit()
        if cur.rowcount == 0:
            raise SecretNotFound(secret_id)

    # ---- versions ----

    def add_version(self, secret_id: str, payload: bytes) -> Dict[str, Any]:
        """Append a new version; returns version metadata."""
        now = time.time()
        with self._lock:
            if not self._conn.execute(
                    "SELECT 1 FROM secrets WHERE secret_id=?",
                    (secret_id,)).fetchone():
                raise SecretNotFound(secret_id)
            row = self._conn.execute(
                "SELECT MAX(version_num) FROM secret_versions WHERE secret_id=?",
                (secret_id,)).fetchone()
            next_num = (row[0] or 0) + 1
            self._conn.execute(
                "INSERT INTO secret_versions VALUES (?,?,?,?,?)",
                (secret_id, next_num, payload, "ENABLED", now))
            self._conn.execute(
                "UPDATE secrets SET updated=? WHERE secret_id=?",
                (now, secret_id))
            self._conn.commit()
        return self._version_dict(secret_id, next_num, "ENABLED", now)

    def get_version(self, secret_id: str,
                    version: str = "latest") -> Dict[str, Any]:
        """Return version metadata (not payload). version = number or 'latest'."""
        ver_num = self._resolve_version(secret_id, version)
        with self._lock:
            row = self._conn.execute(
                "SELECT state, created FROM secret_versions "
                "WHERE secret_id=? AND version_num=?",
                (secret_id, ver_num)).fetchone()
        if row is None:
            raise VersionNotFound(f"{secret_id}/{version}")
        return self._version_dict(secret_id, ver_num, row[0], row[1])

    def list_versions(self, secret_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            if not self._conn.execute(
                    "SELECT 1 FROM secrets WHERE secret_id=?",
                    (secret_id,)).fetchone():
                raise SecretNotFound(secret_id)
            rows = self._conn.execute(
                "SELECT version_num, state, created FROM secret_versions "
                "WHERE secret_id=? ORDER BY version_num",
                (secret_id,)).fetchall()
        return [self._version_dict(secret_id, r[0], r[1], r[2]) for r in rows]

    def access_version(self, secret_id: str,
                       version: str = "latest") -> bytes:
        """Return the raw payload bytes for the version."""
        ver_num = self._resolve_version(secret_id, version)
        with self._lock:
            row = self._conn.execute(
                "SELECT payload, state FROM secret_versions "
                "WHERE secret_id=? AND version_num=?",
                (secret_id, ver_num)).fetchone()
        if row is None:
            raise VersionNotFound(f"{secret_id}/{version}")
        state = row[1]
        if state == "DESTROYED":
            raise VersionStateError(f"version {ver_num} is DESTROYED")
        if state == "DISABLED":
            raise VersionStateError(f"version {ver_num} is DISABLED")
        return bytes(row[0])

    def disable_version(self, secret_id: str, version: str) -> Dict[str, Any]:
        return self._set_version_state(secret_id, version, "DISABLED",
                                       allowed_from={"ENABLED"})

    def enable_version(self, secret_id: str, version: str) -> Dict[str, Any]:
        return self._set_version_state(secret_id, version, "ENABLED",
                                       allowed_from={"DISABLED"})

    def destroy_version(self, secret_id: str, version: str) -> Dict[str, Any]:
        ver_num = self._resolve_version(secret_id, version)
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT state FROM secret_versions "
                "WHERE secret_id=? AND version_num=?",
                (secret_id, ver_num)).fetchone()
            if row is None:
                raise VersionNotFound(f"{secret_id}/{version}")
            if row[0] == "DESTROYED":
                raise VersionStateError("version already DESTROYED")
            # On destroy, wipe the payload and null it out
            self._conn.execute(
                "UPDATE secret_versions SET state='DESTROYED', payload='' "
                "WHERE secret_id=? AND version_num=?",
                (secret_id, ver_num))
            self._conn.commit()
        return self._version_dict(secret_id, ver_num, "DESTROYED", now)

    # ---- helpers ----

    def _resolve_version(self, secret_id: str, version: str) -> int:
        if version == "latest":
            with self._lock:
                row = self._conn.execute(
                    "SELECT MAX(version_num) FROM secret_versions "
                    "WHERE secret_id=? AND state != 'DESTROYED'",
                    (secret_id,)).fetchone()
            if row is None or row[0] is None:
                raise VersionNotFound(f"{secret_id}/latest")
            return row[0]
        try:
            return int(version)
        except ValueError:
            raise VersionNotFound(f"{secret_id}/{version}")

    def _set_version_state(self, secret_id: str, version: str, new_state: str,
                           allowed_from: set) -> Dict[str, Any]:
        ver_num = self._resolve_version(secret_id, version)
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT state FROM secret_versions "
                "WHERE secret_id=? AND version_num=?",
                (secret_id, ver_num)).fetchone()
            if row is None:
                raise VersionNotFound(f"{secret_id}/{version}")
            if row[0] not in allowed_from:
                raise VersionStateError(
                    f"cannot transition from {row[0]} to {new_state}")
            self._conn.execute(
                "UPDATE secret_versions SET state=? "
                "WHERE secret_id=? AND version_num=?",
                (new_state, secret_id, ver_num))
            self._conn.commit()
        return self._version_dict(secret_id, ver_num, new_state, now)

    @staticmethod
    def _secret_dict(secret_id: str, labels: Dict[str, str],
                     created: float) -> Dict[str, Any]:
        return {
            "name": f"projects/local/secrets/{secret_id}",
            "secretId": secret_id,
            "labels": labels,
            "createTime": created,
        }

    @staticmethod
    def _version_dict(secret_id: str, version_num: int, state: str,
                      created: float) -> Dict[str, Any]:
        return {
            "name": f"projects/local/secrets/{secret_id}/versions/{version_num}",
            "secretId": secret_id,
            "version": version_num,
            "state": state,
            "createTime": created,
        }
