"""Identity Platform-style authentication service.

Implements a compatible SUBSET of Firebase / Identity Platform Auth:
  * Sign-up (create user) with email + password.
  * Sign-in with email + password; returns a short-lived ID token (JWT-like).
  * ID token verification — parse, verify HMAC-SHA256 signature, check expiry.
  * Get / update / delete user by UID.
  * List users.
  * Custom token creation for server-to-server flows (same HMAC scheme).

Token format (opengcp-local, NOT standard JWT):
  base64url(header) . base64url(payload) . base64url(HMAC-SHA256-signature)

The signing secret is generated fresh per-instance (in-memory default) or
seeded from the ``secret`` constructor parameter.

This is an independent reimplementation for LOCAL development. It is NOT
affiliated with or endorsed by Google.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

# Token validity window in seconds (1 hour)
_TOKEN_TTL = 3600


class AuthError(Exception):
    pass


class UserNotFound(AuthError):
    pass


class EmailExistsError(AuthError):
    pass


class InvalidCredentials(AuthError):
    pass


class TokenExpiredError(AuthError):
    pass


class TokenInvalidError(AuthError):
    pass


# ---- token helpers ----

def _b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(s: str) -> bytes:
    padded = s + "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(padded)


def _sign(secret: bytes, message: str) -> str:
    sig = hmac.new(secret, message.encode("utf-8"),
                   digestmod=hashlib.sha256).digest()
    return _b64u_encode(sig)


def _make_token(secret: bytes, uid: str, email: str,
                ttl: int = _TOKEN_TTL,
                extra: Optional[Dict[str, Any]] = None) -> str:
    header = _b64u_encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    now = int(time.time())
    payload_dict: Dict[str, Any] = {
        "sub": uid,
        "email": email,
        "iat": now,
        "exp": now + ttl,
    }
    if extra:
        payload_dict.update(extra)
    payload = _b64u_encode(json.dumps(payload_dict).encode())
    msg = f"{header}.{payload}"
    sig = _sign(secret, msg)
    return f"{msg}.{sig}"


def _decode_token(secret: bytes, token: str) -> Dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise TokenInvalidError("malformed token")
    header_b64, payload_b64, sig_b64 = parts
    expected_sig = _sign(secret, f"{header_b64}.{payload_b64}")
    if not hmac.compare_digest(sig_b64, expected_sig):
        raise TokenInvalidError("invalid signature")
    try:
        payload = json.loads(_b64u_decode(payload_b64))
    except Exception as exc:
        raise TokenInvalidError(f"cannot decode payload: {exc}") from exc
    if payload.get("exp", 0) < int(time.time()):
        raise TokenExpiredError("token has expired")
    return payload


def _hash_password(password: str, salt: bytes) -> str:
    """Return hex-encoded PBKDF2-HMAC-SHA256 of password+salt (100k iterations)."""
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return dk.hex()


class IdentityPlatform:
    """Thread-safe Identity Platform / Firebase Auth emulator.

    ``path`` of None (default) uses an in-memory SQLite database.
    ``secret`` seeds the HMAC signing key; random by default.
    """

    def __init__(self, path: Optional[str] = None,
                 secret: Optional[bytes] = None):
        self._lock = threading.RLock()
        self._secret: bytes = secret or os.urandom(32)
        db_path = path or ":memory:"
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    uid          TEXT PRIMARY KEY,
                    email        TEXT UNIQUE NOT NULL,
                    pw_hash      TEXT NOT NULL,
                    pw_salt      TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    disabled     INTEGER NOT NULL DEFAULT 0,
                    created      REAL NOT NULL,
                    updated      REAL NOT NULL
                );
            """)
            self._conn.commit()

    # ---- sign-up / sign-in ----

    def sign_up(self, email: str, password: str,
                display_name: str = "") -> Dict[str, Any]:
        """Create a new user; return user record + ID token."""
        _validate_email(email)
        _validate_password(password)
        now = time.time()
        uid = uuid.uuid4().hex
        salt = os.urandom(16)
        pw_hash = _hash_password(password, salt)
        with self._lock:
            if self._conn.execute(
                    "SELECT 1 FROM users WHERE email=?",
                    (email,)).fetchone():
                raise EmailExistsError(f"email already in use: {email}")
            self._conn.execute(
                "INSERT INTO users VALUES (?,?,?,?,?,0,?,?)",
                (uid, email, pw_hash, salt.hex(),
                 display_name, now, now))
            self._conn.commit()
        token = _make_token(self._secret, uid, email)
        return {
            "uid": uid,
            "email": email,
            "displayName": display_name,
            "idToken": token,
            "expiresIn": _TOKEN_TTL,
        }

    def sign_in(self, email: str, password: str) -> Dict[str, Any]:
        """Authenticate with email+password; return user record + fresh ID token."""
        with self._lock:
            row = self._conn.execute(
                "SELECT uid, pw_hash, pw_salt, display_name, disabled "
                "FROM users WHERE email=?",
                (email,)).fetchone()
        if row is None:
            raise InvalidCredentials("user not found")
        uid, pw_hash, salt_hex, display_name, disabled = row
        if disabled:
            raise AuthError("user account is disabled")
        candidate = _hash_password(password, bytes.fromhex(salt_hex))
        if not hmac.compare_digest(candidate, pw_hash):
            raise InvalidCredentials("wrong password")
        token = _make_token(self._secret, uid, email)
        return {
            "uid": uid,
            "email": email,
            "displayName": display_name,
            "idToken": token,
            "expiresIn": _TOKEN_TTL,
        }

    # ---- token verification ----

    def verify_id_token(self, token: str) -> Dict[str, Any]:
        """Verify an ID token; return the decoded payload dict."""
        return _decode_token(self._secret, token)

    # ---- custom tokens ----

    def create_custom_token(self, uid: str,
                             claims: Optional[Dict[str, Any]] = None,
                             ttl: int = _TOKEN_TTL) -> str:
        """Issue a custom token for ``uid`` with optional extra claims."""
        with self._lock:
            row = self._conn.execute(
                "SELECT email FROM users WHERE uid=?", (uid,)).fetchone()
        if row is None:
            raise UserNotFound(uid)
        return _make_token(self._secret, uid, row[0], ttl=ttl, extra=claims)

    # ---- user management ----

    def get_user(self, uid: str) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT uid, email, display_name, disabled, created "
                "FROM users WHERE uid=?",
                (uid,)).fetchone()
        if row is None:
            raise UserNotFound(uid)
        return _user_dict(*row)

    def get_user_by_email(self, email: str) -> Dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT uid, email, display_name, disabled, created "
                "FROM users WHERE email=?",
                (email,)).fetchone()
        if row is None:
            raise UserNotFound(email)
        return _user_dict(*row)

    def update_user(self, uid: str, *,
                    display_name: Optional[str] = None,
                    password: Optional[str] = None,
                    disabled: Optional[bool] = None) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT pw_salt FROM users WHERE uid=?", (uid,)).fetchone()
            if row is None:
                raise UserNotFound(uid)
            updates: List[str] = ["updated=?"]
            params: List[Any] = [now]
            if display_name is not None:
                updates.append("display_name=?")
                params.append(display_name)
            if disabled is not None:
                updates.append("disabled=?")
                params.append(1 if disabled else 0)
            if password is not None:
                _validate_password(password)
                new_salt = os.urandom(16)
                new_hash = _hash_password(password, new_salt)
                updates.append("pw_hash=?")
                params.append(new_hash)
                updates.append("pw_salt=?")
                params.append(new_salt.hex())
            params.append(uid)
            self._conn.execute(
                f"UPDATE users SET {', '.join(updates)} WHERE uid=?",
                params)
            self._conn.commit()
        return self.get_user(uid)

    def delete_user(self, uid: str) -> None:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM users WHERE uid=?", (uid,))
            self._conn.commit()
        if cur.rowcount == 0:
            raise UserNotFound(uid)

    def list_users(self, max_results: int = 1000) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT uid, email, display_name, disabled, created "
                "FROM users ORDER BY created ASC LIMIT ?",
                (max_results,)).fetchall()
        return [_user_dict(*r) for r in rows]


# ---- helpers ----

def _user_dict(uid: str, email: str, display_name: str,
               disabled: int, created: float) -> Dict[str, Any]:
    return {
        "uid": uid,
        "email": email,
        "displayName": display_name,
        "disabled": bool(disabled),
        "createTime": created,
    }


def _validate_email(email: str) -> None:
    if "@" not in email or len(email) < 3:
        raise AuthError(f"invalid email: {email!r}")


def _validate_password(password: str) -> None:
    if len(password) < 6:
        raise AuthError("password must be at least 6 characters")
