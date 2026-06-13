"""Cloud KMS-style key management service.

Implements a compatible SUBSET of Cloud KMS:
  * KeyRings: create / get / list.
  * CryptoKeys: create / get / list (symmetric encrypt/decrypt algorithm).
  * CryptoKeyVersions: auto-created primary version on key creation; list versions.
  * Encrypt: AES-256-CBC via stdlib + PKCS7 padding (no third-party deps).
  * Decrypt: matching decryption.
  * GenerateDataKey: produce a random data-encryption key (DEK), returned
    plaintext + encrypted-with-master (symmetric wrapping via HMAC-SHA256 +
    XOR cipher for the DEK bytes — stdlib-only).

Algorithm note:
  Pure-stdlib AES is not available, so this emulator uses a *keyed-block
  cipher* built on SHA-256 + HMAC in counter mode (CTR-like). It is NOT
  cryptographically AES; it is an opaque opengcp-local cipher that provides
  the same *interface* as Cloud KMS encrypt/decrypt with ciphertexts only
  decryptable by the same key stored in the emulator.  For real production
  use, use a real KMS.

All data is in-memory.

This is an independent reimplementation for LOCAL development. It is NOT
affiliated with or endorsed by Google.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
import threading
import time
from typing import Any, Dict, List, Optional


class KMSError(Exception):
    pass


class KeyRingNotFound(KMSError):
    pass


class CryptoKeyNotFound(KMSError):
    pass


# ---- stdlib CTR cipher (keyed stream via HMAC-SHA256) ----

def _derive_key(master: bytes, label: bytes = b"") -> bytes:
    """Derive a 32-byte working key via HMAC-SHA256."""
    return hmac.new(master, label, digestmod=hashlib.sha256).digest()


def _ctr_stream(key: bytes, nonce: bytes, length: int) -> bytes:
    """Generate ``length`` pseudo-random bytes using HMAC-SHA256 in counter mode."""
    blocks: List[bytes] = []
    counter = 0
    while len(b"".join(blocks)) < length:
        h = hmac.new(key, nonce + struct.pack(">Q", counter),
                     digestmod=hashlib.sha256).digest()
        blocks.append(h)
        counter += 1
    return b"".join(blocks)[:length]


def _encrypt_payload(key_bytes: bytes, plaintext: bytes) -> bytes:
    """Encrypt plaintext with a 16-byte random nonce prepended."""
    nonce = os.urandom(16)
    stream = _ctr_stream(key_bytes, nonce, len(plaintext))
    ciphertext = bytes(a ^ b for a, b in zip(plaintext, stream))
    # integrity tag: HMAC of nonce+ciphertext
    tag = hmac.new(key_bytes, nonce + ciphertext, digestmod=hashlib.sha256).digest()
    return nonce + tag + ciphertext


def _decrypt_payload(key_bytes: bytes, blob: bytes) -> bytes:
    """Decrypt a blob produced by ``_encrypt_payload``."""
    if len(blob) < 16 + 32:
        raise KMSError("ciphertext too short")
    nonce = blob[:16]
    tag = blob[16:48]
    ciphertext = blob[48:]
    expected_tag = hmac.new(key_bytes, nonce + ciphertext,
                            digestmod=hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected_tag):
        raise KMSError("decryption failed: integrity check mismatch")
    stream = _ctr_stream(key_bytes, nonce, len(ciphertext))
    return bytes(a ^ b for a, b in zip(ciphertext, stream))


class KMSService:
    """Thread-safe Cloud KMS emulator (in-memory)."""

    def __init__(self):
        self._lock = threading.RLock()
        # keyrings: name -> {name, created}
        self._keyrings: Dict[str, Dict[str, Any]] = {}
        # keys: (keyring, key_id) -> {meta, key_material:bytes}
        self._keys: Dict[tuple, Dict[str, Any]] = {}
        # versions: (keyring, key_id, ver_num) -> {state}
        self._versions: Dict[tuple, Dict[str, Any]] = {}

    # ---- keyrings ----

    def create_key_ring(self, key_ring_id: str) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            if key_ring_id in self._keyrings:
                raise KMSError(f"key ring already exists: {key_ring_id}")
            meta = {"name": key_ring_id, "createTime": now}
            self._keyrings[key_ring_id] = meta
        return dict(meta)

    def get_key_ring(self, key_ring_id: str) -> Dict[str, Any]:
        with self._lock:
            if key_ring_id not in self._keyrings:
                raise KeyRingNotFound(key_ring_id)
            return dict(self._keyrings[key_ring_id])

    def list_key_rings(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(v) for v in sorted(self._keyrings.values(),
                                             key=lambda x: x["name"])]

    # ---- crypto keys ----

    def create_crypto_key(self, key_ring_id: str, key_id: str,
                          purpose: str = "ENCRYPT_DECRYPT") -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            if key_ring_id not in self._keyrings:
                raise KeyRingNotFound(key_ring_id)
            k = (key_ring_id, key_id)
            if k in self._keys:
                raise KMSError(f"key already exists: {key_id}")
            # generate a random 32-byte master key material
            key_material = os.urandom(32)
            meta = {
                "name": f"{key_ring_id}/cryptoKeys/{key_id}",
                "keyRingId": key_ring_id,
                "keyId": key_id,
                "purpose": purpose,
                "createTime": now,
                "primaryVersion": 1,
            }
            self._keys[k] = {"meta": meta, "material": key_material}
            self._versions[(key_ring_id, key_id, 1)] = {
                "version": 1, "state": "ENABLED", "createTime": now}
        return dict(meta)

    def get_crypto_key(self, key_ring_id: str, key_id: str) -> Dict[str, Any]:
        with self._lock:
            k = (key_ring_id, key_id)
            if k not in self._keys:
                raise CryptoKeyNotFound(f"{key_ring_id}/{key_id}")
            return dict(self._keys[k]["meta"])

    def list_crypto_keys(self, key_ring_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            if key_ring_id not in self._keyrings:
                raise KeyRingNotFound(key_ring_id)
            return [dict(v["meta"]) for (kr, _), v in self._keys.items()
                    if kr == key_ring_id]

    def list_crypto_key_versions(self, key_ring_id: str,
                                  key_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            if (key_ring_id, key_id) not in self._keys:
                raise CryptoKeyNotFound(f"{key_ring_id}/{key_id}")
            return [dict(v) for (kr, ki, _), v in self._versions.items()
                    if kr == key_ring_id and ki == key_id]

    # ---- encrypt / decrypt ----

    def encrypt(self, key_ring_id: str, key_id: str,
                plaintext: bytes,
                additional_authenticated_data: bytes = b"") -> Dict[str, Any]:
        """Encrypt bytes; returns base64-encoded ciphertext."""
        material = self._get_material(key_ring_id, key_id)
        # mix AAD into the working key derivation so it's bound to the ciphertext
        working_key = _derive_key(material, additional_authenticated_data)
        blob = _encrypt_payload(working_key, plaintext)
        # prepend AAD digest so we can verify on decrypt
        aad_hash = hashlib.sha256(additional_authenticated_data).digest()
        full_blob = aad_hash + blob
        ciphertext_b64 = base64.b64encode(full_blob).decode("ascii")
        return {
            "name": f"{key_ring_id}/cryptoKeys/{key_id}",
            "ciphertext": ciphertext_b64,
        }

    def decrypt(self, key_ring_id: str, key_id: str,
                ciphertext_b64: str,
                additional_authenticated_data: bytes = b"") -> bytes:
        """Decrypt a ciphertext produced by :meth:`encrypt`."""
        material = self._get_material(key_ring_id, key_id)
        try:
            full_blob = base64.b64decode(ciphertext_b64)
        except Exception as exc:
            raise KMSError(f"invalid base64 ciphertext: {exc}") from exc
        if len(full_blob) < 32:
            raise KMSError("ciphertext too short")
        aad_hash_stored = full_blob[:32]
        aad_hash_expected = hashlib.sha256(additional_authenticated_data).digest()
        if not hmac.compare_digest(aad_hash_stored, aad_hash_expected):
            raise KMSError(
                "decryption failed: additional authenticated data mismatch")
        working_key = _derive_key(material, additional_authenticated_data)
        return _decrypt_payload(working_key, full_blob[32:])

    def generate_data_key(self, key_ring_id: str,
                          key_id: str) -> Dict[str, Any]:
        """Generate a random 32-byte DEK, return it plaintext + wrapped.

        The wrapped form is an encrypt() of the plaintext DEK under the KMS
        key so that the plaintext can be re-derived by calling decrypt().
        """
        dek = os.urandom(32)
        encrypted = self.encrypt(key_ring_id, key_id, dek)
        return {
            "plaintext": base64.b64encode(dek).decode("ascii"),
            "ciphertextBlob": encrypted["ciphertext"],
        }

    # ---- helpers ----

    def _get_material(self, key_ring_id: str, key_id: str) -> bytes:
        with self._lock:
            k = (key_ring_id, key_id)
            if k not in self._keys:
                raise CryptoKeyNotFound(f"{key_ring_id}/{key_id}")
            return self._keys[k]["material"]
