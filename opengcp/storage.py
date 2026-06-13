"""GCS-compatible object storage backed by the local filesystem (or memory).

Implements a compatible SUBSET of the Google Cloud Storage object model:
buckets that contain objects, each object addressed by name, with metadata
(size, md5, content-type, generation, time-created, custom metadata).
Object names may contain '/' which is treated as part of the key (GCS has no
real directories).

This pass adds:
  - Custom object metadata (user-defined key/value pairs).
  - copy_object: server-side copy (with optional rename).
  - compose: concatenate up to 32 source objects into one destination.
  - list_objects with delimiter support (simulated directory listing, returning
    ``prefixes`` as well as ``items``).
  - Object versioning per bucket: when enabled, delete does a soft-delete
    (noncurrentTime set) and old generations are kept; list shows only live
    objects by default (pass ``versions=True`` to include noncurrent).
  - Bucket lifecycle stub: attach a lifecycle rules dict to the bucket; opengcp
    does NOT enforce them automatically (same as "stub"), but they round-trip
    through the API.

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
from typing import Dict, List, Optional, Tuple


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
    # New in storage+data pass
    custom_metadata: Dict[str, str] = field(default_factory=dict)
    # noncurrent_time is non-None when this is a versioned (soft-deleted) object
    noncurrent_time: Optional[float] = None

    def to_dict(self) -> dict:
        d = {
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
        if self.custom_metadata:
            d["metadata"] = dict(self.custom_metadata)
        if self.noncurrent_time is not None:
            d["noncurrentTime"] = self.noncurrent_time
            d["timeDeleted"] = self.noncurrent_time
        return d


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
        # live objects: bucket -> name -> bytes
        self._objects: Dict[str, Dict[str, bytes]] = {}
        # live metadata: bucket -> name -> ObjectMeta
        self._meta: Dict[str, Dict[str, ObjectMeta]] = {}
        # versioned (noncurrent) objects: bucket -> list[ObjectMeta]
        # stored separately from live objects; data is keyed by gen_key()
        self._versions: Dict[str, List[ObjectMeta]] = {}
        # version data: bucket -> gen_key -> bytes
        self._version_data: Dict[str, Dict[str, bytes]] = {}
        if self._root:
            os.makedirs(self._root, exist_ok=True)
            self._load()

    @staticmethod
    def _gen_key(name: str, generation: int) -> str:
        return f"{_safe_key(name)}.gen{generation}"

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
            self._versions[name] = []
            self._version_data[name] = {}
            for fn in os.listdir(bdir):
                if not fn.endswith(".meta.json"):
                    continue
                with open(os.path.join(bdir, fn), "r", encoding="utf-8") as fh:
                    m = json.load(fh)
                meta = self._meta_from_dict(m)
                data_path = os.path.join(bdir, fn[:-len(".meta.json")] + ".data")
                if not os.path.exists(data_path):
                    continue
                with open(data_path, "rb") as fh:
                    raw = fh.read()
                # noncurrent objects are stored with gen suffix in filename
                if meta.noncurrent_time is not None:
                    self._versions[name].append(meta)
                    gk = self._gen_key(meta.name, meta.generation)
                    self._version_data[name][gk] = raw
                else:
                    self._meta[name][meta.name] = meta
                    self._objects[name][meta.name] = raw

    @staticmethod
    def _meta_from_dict(m: dict) -> "ObjectMeta":
        return ObjectMeta(
            bucket=m["bucket"],
            name=m["name"],
            size=m["size"],
            md5_hash=m["md5_hash"],
            content_type=m["content_type"],
            generation=m["generation"],
            time_created=m["time_created"],
            updated=m["updated"],
            custom_metadata=m.get("custom_metadata", {}),
            noncurrent_time=m.get("noncurrent_time"),
        )

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

    def _persist_noncurrent(self, bucket: str, meta: ObjectMeta, data: bytes) -> None:
        if not self._root:
            return
        bdir = self._bucket_dir(bucket)
        os.makedirs(bdir, exist_ok=True)
        gk = self._gen_key(meta.name, meta.generation)
        with open(os.path.join(bdir, gk + ".data"), "wb") as fh:
            fh.write(data)
        with open(os.path.join(bdir, gk + ".meta.json"), "w", encoding="utf-8") as fh:
            json.dump(asdict(meta), fh)

    def _remove_object_files(self, bucket: str, name: str) -> None:
        if not self._root:
            return
        bdir = self._bucket_dir(bucket)
        key = _safe_key(name)
        for suffix in (".data", ".meta.json"):
            p = os.path.join(bdir, key + suffix)
            if os.path.exists(p):
                os.remove(p)

    def _remove_noncurrent_files(self, bucket: str, meta: ObjectMeta) -> None:
        if not self._root:
            return
        bdir = self._bucket_dir(bucket)
        gk = self._gen_key(meta.name, meta.generation)
        for suffix in (".data", ".meta.json"):
            p = os.path.join(bdir, gk + suffix)
            if os.path.exists(p):
                os.remove(p)

    # ----- bucket operations -----
    def create_bucket(self, bucket: str,
                      versioning_enabled: bool = False) -> dict:
        with self._lock:
            if bucket in self._buckets:
                raise BucketAlreadyExists(bucket)
            info: dict = {
                "kind": "storage#bucket",
                "name": bucket,
                "timeCreated": time.time(),
                "versioning": {"enabled": versioning_enabled},
                "lifecycle": {"rules": []},
            }
            self._buckets[bucket] = info
            self._objects[bucket] = {}
            self._meta[bucket] = {}
            self._versions[bucket] = []
            self._version_data[bucket] = {}
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
            for vmeta in self._versions.get(bucket, []):
                self._remove_noncurrent_files(bucket, vmeta)
            del self._buckets[bucket]
            self._objects.pop(bucket, None)
            self._meta.pop(bucket, None)
            self._versions.pop(bucket, None)
            self._version_data.pop(bucket, None)
            if self._root:
                bdir = self._bucket_dir(bucket)
                info = os.path.join(bdir, "_bucket.json")
                if os.path.exists(info):
                    os.remove(info)
                if os.path.isdir(bdir) and not os.listdir(bdir):
                    os.rmdir(bdir)

    def set_versioning(self, bucket: str, enabled: bool) -> dict:
        """Enable or disable object versioning on a bucket."""
        with self._lock:
            if bucket not in self._buckets:
                raise BucketNotFound(bucket)
            self._buckets[bucket]["versioning"] = {"enabled": enabled}
            self._persist_bucket(bucket)
            return dict(self._buckets[bucket])

    def set_lifecycle(self, bucket: str, rules: list) -> dict:
        """Attach lifecycle rules (stub — stored but not enforced)."""
        with self._lock:
            if bucket not in self._buckets:
                raise BucketNotFound(bucket)
            self._buckets[bucket]["lifecycle"] = {"rules": list(rules)}
            self._persist_bucket(bucket)
            return dict(self._buckets[bucket])

    # ----- object operations -----
    def _versioning_on(self, bucket: str) -> bool:
        return bool(self._buckets[bucket].get("versioning", {}).get("enabled"))

    def upload(self, bucket: str, name: str, data: bytes,
               content_type: str = "application/octet-stream",
               metadata: Optional[Dict[str, str]] = None) -> ObjectMeta:
        if isinstance(data, str):
            data = data.encode("utf-8")
        with self._lock:
            if bucket not in self._buckets:
                raise BucketNotFound(bucket)
            now = time.time()
            prev = self._meta[bucket].get(name)
            generation = (prev.generation + 1) if prev else 1
            md5 = base64.b64encode(hashlib.md5(data).digest()).decode("ascii")
            new_meta = ObjectMeta(
                bucket=bucket, name=name, size=len(data), md5_hash=md5,
                content_type=content_type, generation=generation,
                time_created=(prev.time_created if prev else now), updated=now,
                custom_metadata=dict(metadata or {}),
            )
            # If versioning is ON and there's a previous live object, archive it
            if prev is not None and self._versioning_on(bucket):
                prev_data = self._objects[bucket][name]
                archived = ObjectMeta(
                    bucket=prev.bucket, name=prev.name, size=prev.size,
                    md5_hash=prev.md5_hash, content_type=prev.content_type,
                    generation=prev.generation,
                    time_created=prev.time_created, updated=prev.updated,
                    custom_metadata=dict(prev.custom_metadata),
                    noncurrent_time=now,
                )
                self._versions[bucket].append(archived)
                gk = self._gen_key(prev.name, prev.generation)
                self._version_data[bucket][gk] = prev_data
                self._persist_noncurrent(bucket, archived, prev_data)
                # remove old live files
                self._remove_object_files(bucket, name)
            self._objects[bucket][name] = data
            self._meta[bucket][name] = new_meta
            self._persist_object(bucket, name)
            return new_meta

    def download(self, bucket: str, name: str,
                 generation: Optional[int] = None) -> bytes:
        with self._lock:
            if bucket not in self._buckets:
                raise BucketNotFound(bucket)
            if generation is None:
                if name not in self._objects[bucket]:
                    raise ObjectNotFound(name)
                return self._objects[bucket][name]
            # versioned download
            if name in self._meta[bucket] and self._meta[bucket][name].generation == generation:
                return self._objects[bucket][name]
            for vmeta in self._versions.get(bucket, []):
                if vmeta.name == name and vmeta.generation == generation:
                    gk = self._gen_key(name, generation)
                    return self._version_data[bucket][gk]
            raise ObjectNotFound(f"{name}#{generation}")

    def stat(self, bucket: str, name: str,
             generation: Optional[int] = None) -> ObjectMeta:
        with self._lock:
            if bucket not in self._buckets:
                raise BucketNotFound(bucket)
            if generation is None:
                if name not in self._meta[bucket]:
                    raise ObjectNotFound(name)
                return self._meta[bucket][name]
            if name in self._meta[bucket] and self._meta[bucket][name].generation == generation:
                return self._meta[bucket][name]
            for vmeta in self._versions.get(bucket, []):
                if vmeta.name == name and vmeta.generation == generation:
                    return vmeta
            raise ObjectNotFound(f"{name}#{generation}")

    def list_objects(self, bucket: str, prefix: str = "",
                     delimiter: str = "",
                     versions: bool = False) -> Tuple[List[ObjectMeta], List[str]]:
        """Return ``(items, prefixes)``.

        ``items`` are live :class:`ObjectMeta` objects whose name starts with
        ``prefix``.  If ``delimiter`` is given, names that contain the delimiter
        *after* the prefix are not included in items; instead the common prefix
        up-to-and-including the delimiter is collected in ``prefixes`` (unique,
        sorted).  When ``versions=True`` noncurrent objects are appended to
        ``items`` as well.
        """
        with self._lock:
            if bucket not in self._buckets:
                raise BucketNotFound(bucket)
            items: List[ObjectMeta] = []
            prefixes: List[str] = []
            seen_prefixes: set = set()
            for name, meta in sorted(self._meta[bucket].items()):
                if not name.startswith(prefix):
                    continue
                if delimiter:
                    rest = name[len(prefix):]
                    idx = rest.find(delimiter)
                    if idx >= 0:
                        cp = prefix + rest[: idx + len(delimiter)]
                        if cp not in seen_prefixes:
                            seen_prefixes.add(cp)
                            prefixes.append(cp)
                        continue
                items.append(meta)
            if versions:
                for vmeta in self._versions.get(bucket, []):
                    if vmeta.name.startswith(prefix):
                        if delimiter:
                            rest = vmeta.name[len(prefix):]
                            idx = rest.find(delimiter)
                            if idx >= 0:
                                continue
                        items.append(vmeta)
                items.sort(key=lambda m: (m.name, m.generation))
            return items, sorted(prefixes)

    def delete(self, bucket: str, name: str) -> None:
        with self._lock:
            if bucket not in self._buckets:
                raise BucketNotFound(bucket)
            if name not in self._objects[bucket]:
                raise ObjectNotFound(name)
            if self._versioning_on(bucket):
                # soft-delete: move to noncurrent
                now = time.time()
                meta = self._meta[bucket][name]
                archived = ObjectMeta(
                    bucket=meta.bucket, name=meta.name, size=meta.size,
                    md5_hash=meta.md5_hash, content_type=meta.content_type,
                    generation=meta.generation,
                    time_created=meta.time_created, updated=meta.updated,
                    custom_metadata=dict(meta.custom_metadata),
                    noncurrent_time=now,
                )
                data = self._objects[bucket][name]
                self._versions[bucket].append(archived)
                gk = self._gen_key(meta.name, meta.generation)
                self._version_data[bucket][gk] = data
                self._persist_noncurrent(bucket, archived, data)
                del self._objects[bucket][name]
                del self._meta[bucket][name]
                self._remove_object_files(bucket, name)
            else:
                del self._objects[bucket][name]
                del self._meta[bucket][name]
                self._remove_object_files(bucket, name)

    def delete_version(self, bucket: str, name: str, generation: int) -> None:
        """Permanently delete a specific (potentially noncurrent) version."""
        with self._lock:
            if bucket not in self._buckets:
                raise BucketNotFound(bucket)
            # check live object
            if (name in self._meta[bucket]
                    and self._meta[bucket][name].generation == generation):
                del self._objects[bucket][name]
                del self._meta[bucket][name]
                self._remove_object_files(bucket, name)
                return
            # check versions
            for i, vmeta in enumerate(self._versions.get(bucket, [])):
                if vmeta.name == name and vmeta.generation == generation:
                    self._versions[bucket].pop(i)
                    gk = self._gen_key(name, generation)
                    self._version_data[bucket].pop(gk, None)
                    self._remove_noncurrent_files(bucket, vmeta)
                    return
            raise ObjectNotFound(f"{name}#{generation}")

    def copy_object(self, src_bucket: str, src_name: str,
                    dst_bucket: str, dst_name: str,
                    metadata: Optional[Dict[str, str]] = None) -> ObjectMeta:
        """Server-side copy (src -> dst).  Copies data and metadata; optionally
        replaces custom metadata.  Source and destination may be the same bucket.
        """
        with self._lock:
            data = self.download(src_bucket, src_name)
            src_meta = self.stat(src_bucket, src_name)
            return self.upload(
                dst_bucket, dst_name, data,
                content_type=src_meta.content_type,
                metadata=metadata if metadata is not None else dict(src_meta.custom_metadata),
            )

    def compose(self, bucket: str, dst_name: str,
                src_names: List[str],
                content_type: str = "application/octet-stream") -> ObjectMeta:
        """Concatenate up to 32 source objects in ``bucket`` into ``dst_name``.

        Mirrors GCS compose.  All sources must already exist in ``bucket``.
        """
        if not src_names:
            raise StorageError("compose: need at least one source object")
        if len(src_names) > 32:
            raise StorageError("compose: maximum 32 source objects")
        with self._lock:
            parts: List[bytes] = []
            for n in src_names:
                parts.append(self.download(bucket, n))
            return self.upload(bucket, dst_name, b"".join(parts),
                               content_type=content_type)

    def update_metadata(self, bucket: str, name: str,
                        metadata: Dict[str, str]) -> ObjectMeta:
        """Replace custom metadata on a live object."""
        with self._lock:
            if bucket not in self._buckets:
                raise BucketNotFound(bucket)
            if name not in self._meta[bucket]:
                raise ObjectNotFound(name)
            meta = self._meta[bucket][name]
            meta.custom_metadata = dict(metadata)
            meta.updated = time.time()
            self._persist_object(bucket, name)
            return meta
