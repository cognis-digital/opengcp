"""GCS-compatible object storage backed by the local filesystem (or memory).

Implements a compatible SUBSET of the Google Cloud Storage object model:
buckets that contain objects, each object addressed by name, with metadata
(size, md5, content-type, generation, time-created). Object names may contain
'/' which is treated as part of the key (GCS has no real directories).

This is an independent reimplementation for LOCAL development. It is NOT
affiliated with or endorsed by Google.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional


class StorageError(Exception):
    """Base class for storage errors."""


class BucketNotFound(StorageError):
    pass


class BucketAlreadyExists(StorageError):
    pass


class ObjectNotFound(StorageError):
    pass


@dataclass
class ObjectMeta:
    bucket: str
    name: str
    size: int
    md5_hash: str
    content_type: str
    generation: int
    time_created: float
    updated: float

    def to_dict(self) -> dict:
        return {
            "kind": "storage#object",
            "id": f"{self.bucket}/{self.name}/{self.generation}",
            "bucket": self.bucket,
            "name": self.name,
            "size": self.size,
            "md5Hash": self.md5_hash,
            "contentType": self.content_type,
            "generation": self.generation,
            "timeCreated": self.time_created,
            "updated": self.updated,
        }


def _safe_key(name: str) -> str:
    """Map an arbitrary object name to a filesystem-safe relative path.

    GCS object names are flat keys; we encode them so that '..', leading
    slashes and other path tricks cannot escape the bucket directory.
    """
    return base64.urlsafe_b64encode(name.encode("utf-8")).decode("ascii")


class ObjectStorage:
    """Thread-safe object storage.

    If ``root`` is None the storage is purely in-memory (ideal for tests).
    Otherwise objects are persisted under ``root`` as files plus a JSON
    metadata sidecar per object.
    """

    def __init__(self, root: Optional[str] = None):
        self._lock = threading.RLock()
        self._root = root
        # in-memory structures (always present; act as cache/source of truth
        # for the in-memory backend, and as an index for the FS backend)
        self._buckets: Dict[str, dict] = {}
        self._objects: Dict[str, Dict[str, bytes]] = {}
        self._meta: Dict[str, Dict[str, ObjectMeta]] = {}
        if self._root:
            os.makedirs(self._root, exist_ok=True)
            self._load()

    # ----- persistence helpers -----
    def _bucket_dir(self, bucket: str) -> str:
        return os.path.join(self._root, _safe_key(bucket))

    def _load(self) -> None:
        for entry in os.listdir(self._root):
            bdir = os.path.join(self._root, entry)
            if not os.path.isdir(bdir):
                continue
            binfo_path = os.path.join(bdir, "_bucket.json")
            if not os.path.exists(binfo_path):
                continue
            with open(binfo_path, "r", encoding="utf-8") as fh:
                binfo = json.load(fh)
            name = binfo["name"]
            self._buckets[name] = binfo
            self._objects[name] = {}
            self._meta[name] = {}
            for fn in os.listdir(bdir):
                if not fn.endswith(".meta.json"):
                    continue
                with open(os.path.join(bdir, fn), "r", encoding="utf-8") as fh:
                    m = json.load(fh)
                meta = ObjectMeta(**{k: m[k] for k in (
                    "bucket", "name", "size", "md5_hash", "content_type",
                    "generation", "time_created", "updated")})
                self._meta[name][meta.name] = meta
                data_path = os.path.join(bdir, fn[:-len(".meta.json")] + ".data")
                with open(data_path, "rb") as fh:
                    self._objects[name][meta.name] = fh.read()

    def _persist_bucket(self, bucket: str) -> None:
        if not self._root:
            return
        bdir = self._bucket_dir(bucket)
        os.makedirs(bdir, exist_ok=True)
        with open(os.path.join(bdir, "_bucket.json"), "w", encoding="utf-8") as fh:
            json.dump(self._buckets[bucket], fh)

    def _persist_object(self, bucket: str, name: str) -> None:
        if not self._root:
            return
        bdir = self._bucket_dir(bucket)
        os.makedirs(bdir, exist_ok=True)
        key = _safe_key(name)
        with open(os.path.join(bdir, key + ".data"), "wb") as fh:
            fh.write(self._objects[bucket][name])
        with open(os.path.join(bdir, key + ".meta.json"), "w", encoding="utf-8") as fh:
            json.dump(asdict(self._meta[bucket][name]), fh)

    def _remove_object_files(self, bucket: str, name: str) -> None:
        if not self._root:
            return
        bdir = self._bucket_dir(bucket)
        key = _safe_key(name)
        for suffix in (".data", ".meta.json"):
            p = os.path.join(bdir, key + suffix)
            if os.path.exists(p):
                os.remove(p)

    # ----- bucket operations -----
    def create_bucket(self, bucket: str) -> dict:
        with self._lock:
            if bucket in self._buckets:
                raise BucketAlreadyExists(bucket)
            info = {
                "kind": "storage#bucket",
                "name": bucket,
                "timeCreated": time.time(),
            }
            self._buckets[bucket] = info
            self._objects[bucket] = {}
            self._meta[bucket] = {}
            self._persist_bucket(bucket)
            return dict(info)

    def get_bucket(self, bucket: str) -> dict:
        with self._lock:
            if bucket not in self._buckets:
                raise BucketNotFound(bucket)
            return dict(self._buckets[bucket])

    def list_buckets(self) -> List[dict]:
        with self._lock:
            return [dict(b) for b in self._buckets.values()]

    def delete_bucket(self, bucket: str) -> None:
        with self._lock:
            if bucket not in self._buckets:
                raise BucketNotFound(bucket)
            for name in list(self._objects.get(bucket, {})):
                self._remove_object_files(bucket, name)
            del self._buckets[bucket]
            self._objects.pop(bucket, None)
            self._meta.pop(bucket, None)
            if self._root:
                bdir = self._bucket_dir(bucket)
                info = os.path.join(bdir, "_bucket.json")
                if os.path.exists(info):
                    os.remove(info)
                if os.path.isdir(bdir) and not os.listdir(bdir):
                    os.rmdir(bdir)

    # ----- object operations -----
    def upload(self, bucket: str, name: str, data: bytes,
               content_type: str = "application/octet-stream") -> ObjectMeta:
        if isinstance(data, str):
            data = data.encode("utf-8")
        with self._lock:
            if bucket not in self._buckets:
                raise BucketNotFound(bucket)
            now = time.time()
            prev = self._meta[bucket].get(name)
            generation = (prev.generation + 1) if prev else 1
            md5 = base64.b64encode(hashlib.md5(data).digest()).decode("ascii")
            meta = ObjectMeta(
                bucket=bucket, name=name, size=len(data), md5_hash=md5,
                content_type=content_type, generation=generation,
                time_created=(prev.time_created if prev else now), updated=now,
            )
            self._objects[bucket][name] = data
            self._meta[bucket][name] = meta
            self._persist_object(bucket, name)
            return meta

    def download(self, bucket: str, name: str) -> bytes:
        with self._lock:
            if bucket not in self._buckets:
                raise BucketNotFound(bucket)
            if name not in self._objects[bucket]:
                raise ObjectNotFound(name)
            return self._objects[bucket][name]

    def stat(self, bucket: str, name: str) -> ObjectMeta:
        with self._lock:
            if bucket not in self._buckets:
                raise BucketNotFound(bucket)
            if name not in self._meta[bucket]:
                raise ObjectNotFound(name)
            return self._meta[bucket][name]

    def list_objects(self, bucket: str, prefix: str = "") -> List[ObjectMeta]:
        with self._lock:
            if bucket not in self._buckets:
                raise BucketNotFound(bucket)
            return [m for n, m in sorted(self._meta[bucket].items())
                    if n.startswith(prefix)]

    def delete(self, bucket: str, name: str) -> None:
        with self._lock:
            if bucket not in self._buckets:
                raise BucketNotFound(bucket)
            if name not in self._objects[bucket]:
                raise ObjectNotFound(name)
            del self._objects[bucket][name]
            del self._meta[bucket][name]
            self._remove_object_files(bucket, name)
