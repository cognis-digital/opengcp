"""Cloud IAM-style identity and access management.

Implements a compatible SUBSET of Cloud IAM primitives:
  * Resource registration (projects, services, custom resources).
  * Role definitions (predefined + custom) with permission sets.
  * Policy bindings: getIamPolicy / setIamPolicy per resource.
  * testIamPermissions — check which permissions a principal holds on a resource.

All data is in-memory; pass ``path`` to persist with SQLite.

This is an independent reimplementation for LOCAL development. It is NOT
affiliated with or endorsed by Google.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Set


class IAMError(Exception):
    pass


class RoleNotFound(IAMError):
    pass


class ResourceNotFound(IAMError):
    pass


# ---- built-in predefined roles ----

_PREDEFINED_ROLES: Dict[str, Dict[str, Any]] = {
    "roles/viewer": {
        "title": "Viewer",
        "description": "Read-only access to all resources.",
        "permissions": [
            "storage.objects.get",
            "storage.objects.list",
            "storage.buckets.get",
            "storage.buckets.list",
            "secretmanager.secrets.get",
            "secretmanager.secrets.list",
            "iam.roles.get",
            "iam.roles.list",
            "logging.logEntries.list",
            "monitoring.timeSeries.list",
        ],
        "stage": "GA",
    },
    "roles/editor": {
        "title": "Editor",
        "description": "Read-write access to all resources.",
        "permissions": [
            "storage.objects.get",
            "storage.objects.list",
            "storage.objects.create",
            "storage.objects.delete",
            "storage.buckets.get",
            "storage.buckets.list",
            "storage.buckets.create",
            "secretmanager.secrets.get",
            "secretmanager.secrets.list",
            "secretmanager.secrets.create",
            "secretmanager.versions.access",
            "secretmanager.versions.add",
            "iam.roles.get",
            "iam.roles.list",
            "logging.logEntries.list",
            "logging.logEntries.create",
            "monitoring.timeSeries.list",
            "monitoring.timeSeries.create",
        ],
        "stage": "GA",
    },
    "roles/owner": {
        "title": "Owner",
        "description": "Full access to all resources.",
        "permissions": [
            "storage.objects.get",
            "storage.objects.list",
            "storage.objects.create",
            "storage.objects.delete",
            "storage.buckets.get",
            "storage.buckets.list",
            "storage.buckets.create",
            "storage.buckets.delete",
            "secretmanager.secrets.get",
            "secretmanager.secrets.list",
            "secretmanager.secrets.create",
            "secretmanager.secrets.delete",
            "secretmanager.versions.access",
            "secretmanager.versions.add",
            "secretmanager.versions.disable",
            "secretmanager.versions.destroy",
            "iam.roles.get",
            "iam.roles.list",
            "iam.roles.create",
            "iam.roles.update",
            "iam.roles.delete",
            "iam.serviceAccounts.get",
            "iam.serviceAccounts.create",
            "iam.policies.get",
            "iam.policies.set",
            "logging.logEntries.list",
            "logging.logEntries.create",
            "monitoring.timeSeries.list",
            "monitoring.timeSeries.create",
            "cloudkms.keyRings.get",
            "cloudkms.keyRings.list",
            "cloudkms.keyRings.create",
            "cloudkms.cryptoKeys.get",
            "cloudkms.cryptoKeys.list",
            "cloudkms.cryptoKeys.create",
            "cloudkms.cryptoKeyVersions.useToEncrypt",
            "cloudkms.cryptoKeyVersions.useToDecrypt",
        ],
        "stage": "GA",
    },
    "roles/secretmanager.secretAccessor": {
        "title": "Secret Manager Secret Accessor",
        "description": "Allows accessing secret versions.",
        "permissions": ["secretmanager.versions.access"],
        "stage": "GA",
    },
    "roles/cloudkms.cryptoKeyEncrypterDecrypter": {
        "title": "Cloud KMS CryptoKey Encrypter/Decrypter",
        "description": "Encrypt/decrypt using KMS keys.",
        "permissions": [
            "cloudkms.cryptoKeyVersions.useToEncrypt",
            "cloudkms.cryptoKeyVersions.useToDecrypt",
        ],
        "stage": "GA",
    },
}


class IAMService:
    """Thread-safe Cloud IAM emulator.

    ``path`` of None (default) uses an in-memory SQLite database.
    """

    def __init__(self, path: Optional[str] = None):
        self._lock = threading.RLock()
        db_path = path or ":memory:"
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()
        self._seed_predefined_roles()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS roles (
                    role_id     TEXT PRIMARY KEY,
                    title       TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    permissions TEXT NOT NULL DEFAULT '[]',
                    stage       TEXT NOT NULL DEFAULT 'GA',
                    deleted     INTEGER NOT NULL DEFAULT 0,
                    created     REAL NOT NULL,
                    updated     REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS resources (
                    resource_name TEXT PRIMARY KEY,
                    resource_type TEXT NOT NULL,
                    created       REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS policies (
                    resource_name TEXT NOT NULL,
                    role          TEXT NOT NULL,
                    member        TEXT NOT NULL,
                    PRIMARY KEY (resource_name, role, member)
                );
            """)
            self._conn.commit()

    def _seed_predefined_roles(self) -> None:
        now = time.time()
        with self._lock:
            for rid, meta in _PREDEFINED_ROLES.items():
                existing = self._conn.execute(
                    "SELECT 1 FROM roles WHERE role_id=?", (rid,)).fetchone()
                if not existing:
                    self._conn.execute(
                        "INSERT INTO roles VALUES (?,?,?,?,?,0,?,?)",
                        (rid, meta["title"], meta["description"],
                         json.dumps(meta["permissions"]), meta["stage"],
                         now, now))
            self._conn.commit()

    # ---- roles ----

    def create_role(self, role_id: str, title: str = "", description: str = "",
                    permissions: Optional[List[str]] = None,
                    stage: str = "ALPHA") -> Dict[str, Any]:
        """Create a custom role."""
        now = time.time()
        perms = permissions or []
        with self._lock:
            if self._conn.execute(
                    "SELECT 1 FROM roles WHERE role_id=? AND deleted=0",
                    (role_id,)).fetchone():
                raise IAMError(f"role already exists: {role_id}")
            self._conn.execute(
                "INSERT OR REPLACE INTO roles VALUES (?,?,?,?,?,0,?,?)",
                (role_id, title or role_id, description,
                 json.dumps(perms), stage, now, now))
            self._conn.commit()
        return self._role_dict(role_id, title or role_id, description, perms,
                               stage, now)

    def get_role(self, role_id: str) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT role_id,title,description,permissions,stage,created "
                "FROM roles WHERE role_id=? AND deleted=0",
                (role_id,)).fetchone()
        if row is None:
            raise RoleNotFound(role_id)
        return self._role_dict(row[0], row[1], row[2],
                               json.loads(row[3]), row[4], row[5])

    def list_roles(self, show_deleted: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            if show_deleted:
                rows = self._conn.execute(
                    "SELECT role_id,title,description,permissions,stage,created "
                    "FROM roles ORDER BY role_id").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT role_id,title,description,permissions,stage,created "
                    "FROM roles WHERE deleted=0 ORDER BY role_id").fetchall()
        return [self._role_dict(r[0], r[1], r[2], json.loads(r[3]), r[4], r[5])
                for r in rows]

    def update_role(self, role_id: str, *,
                    title: Optional[str] = None,
                    description: Optional[str] = None,
                    permissions: Optional[List[str]] = None) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT title,description,permissions,stage FROM roles "
                "WHERE role_id=? AND deleted=0",
                (role_id,)).fetchone()
            if row is None:
                raise RoleNotFound(role_id)
            new_title = title if title is not None else row[0]
            new_desc = description if description is not None else row[1]
            new_perms = permissions if permissions is not None else json.loads(row[2])
            self._conn.execute(
                "UPDATE roles SET title=?,description=?,permissions=?,updated=? "
                "WHERE role_id=?",
                (new_title, new_desc, json.dumps(new_perms), now, role_id))
            self._conn.commit()
        return self._role_dict(role_id, new_title, new_desc, new_perms, row[3],
                               time.time())

    def delete_role(self, role_id: str) -> None:
        """Soft-delete a custom role (predefined roles cannot be deleted)."""
        if role_id in _PREDEFINED_ROLES:
            raise IAMError(f"cannot delete predefined role: {role_id}")
        with self._lock:
            cur = self._conn.execute(
                "UPDATE roles SET deleted=1 WHERE role_id=? AND deleted=0",
                (role_id,))
            self._conn.commit()
        if cur.rowcount == 0:
            raise RoleNotFound(role_id)

    # ---- resources ----

    def register_resource(self, resource_name: str,
                          resource_type: str = "generic") -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO resources VALUES (?,?,?)",
                (resource_name, resource_type, now))
            self._conn.commit()
        return {"name": resource_name, "type": resource_type}

    def list_resources(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT resource_name, resource_type FROM resources "
                "ORDER BY resource_name").fetchall()
        return [{"name": r[0], "type": r[1]} for r in rows]

    # ---- policy ----

    def get_iam_policy(self, resource: str) -> Dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, member FROM policies WHERE resource_name=? "
                "ORDER BY role, member",
                (resource,)).fetchall()
        # aggregate into bindings
        bindings: Dict[str, List[str]] = {}
        for role, member in rows:
            bindings.setdefault(role, []).append(member)
        return {
            "version": 1,
            "bindings": [{"role": r, "members": m}
                         for r, m in sorted(bindings.items())],
            "etag": "v1",
        }

    def set_iam_policy(self, resource: str,
                       bindings: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Replace the full IAM policy for the resource."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM policies WHERE resource_name=?", (resource,))
            for binding in bindings:
                role = binding.get("role", "")
                for member in binding.get("members", []):
                    self._conn.execute(
                        "INSERT OR IGNORE INTO policies VALUES (?,?,?)",
                        (resource, role, member))
            self._conn.commit()
        return self.get_iam_policy(resource)

    def test_iam_permissions(self, resource: str, principal: str,
                              permissions: List[str]) -> List[str]:
        """Return the subset of ``permissions`` that ``principal`` holds."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT role FROM policies WHERE resource_name=? AND member=?",
                (resource, principal)).fetchall()
        # also check wildcard member allUsers / allAuthenticatedUsers
        with self._lock:
            extra = self._conn.execute(
                "SELECT role FROM policies WHERE resource_name=? "
                "AND (member='allUsers' OR member='allAuthenticatedUsers')",
                (resource,)).fetchall()
        granted_roles: Set[str] = {r[0] for r in rows} | {r[0] for r in extra}

        # collect all permissions across granted roles
        held: Set[str] = set()
        for role_id in granted_roles:
            try:
                role = self.get_role(role_id)
                held.update(role.get("permissions", []))
            except RoleNotFound:
                pass
        return [p for p in permissions if p in held]

    # ---- helpers ----

    @staticmethod
    def _role_dict(role_id: str, title: str, description: str,
                   permissions: List[str], stage: str,
                   created: float) -> Dict[str, Any]:
        return {
            "name": role_id,
            "title": title,
            "description": description,
            "permissions": permissions,
            "stage": stage,
            "createTime": created,
        }
