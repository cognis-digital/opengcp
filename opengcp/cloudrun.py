"""Cloud Run-style service deployment and HTTP invocation.

Implements a compatible SUBSET of the Cloud Run model:
  * Deploy a Python callable as a named "service" (``deploy``).
  * Invoke the service by name: opengcp passes an HTTP-shaped request dict to
    the handler and captures the response (status code + body + headers).
  * The service is exposed on the local HTTP server at ``/cloudrun/services/<name>/invoke``,
    which POSTs the raw request body to the registered handler.
  * List/get/delete services; each service tracks invocation count and last status.
  * Concurrency limit: if configured, calls beyond the limit queue or reject.

Request dict shape (passed to the handler):
  {
    "method": "POST",
    "path": "/",
    "headers": {"Content-Type": "application/json", ...},
    "body": b"...",
    "queryParams": {"key": ["val"], ...},
  }

Response: the handler should return a dict with optional keys
  {"status": 200, "headers": {...}, "body": b"..." or "..."}
  or return None (treated as 200 OK with empty body).

This is an independent reimplementation for LOCAL development. It is NOT
affiliated with or endorsed by Google.
"""

from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


class CloudRunError(Exception):
    pass


class ServiceNotFound(CloudRunError):
    pass


@dataclass
class ServiceConfig:
    max_concurrency: int = 80
    timeout: float = 60.0
    min_instances: int = 0
    max_instances: int = 1000


@dataclass
class InvocationRecord:
    timestamp: float
    method: str
    path: str
    status: int
    ok: bool
    error: Optional[str] = None
    latency: float = 0.0


@dataclass
class Service:
    name: str
    region: str = "local"
    config: ServiceConfig = field(default_factory=ServiceConfig)
    state: str = "ACTIVE"   # ACTIVE | INACTIVE
    create_time: float = field(default_factory=time.time)
    invocation_count: int = 0
    last_status: Optional[int] = None
    _handler: Optional[Callable[[dict], Any]] = field(default=None, repr=False)
    _log: List[InvocationRecord] = field(default_factory=list, repr=False)
    _sem: threading.Semaphore = field(default=None, repr=False)

    def __post_init__(self):
        if self._sem is None:
            self._sem = threading.Semaphore(self.config.max_concurrency)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "region": self.region,
            "state": self.state,
            "createTime": self.create_time,
            "invocationCount": self.invocation_count,
            "lastStatus": self.last_status,
            "config": {
                "maxConcurrency": self.config.max_concurrency,
                "timeout": self.config.timeout,
                "minInstances": self.config.min_instances,
                "maxInstances": self.config.max_instances,
            },
        }


class CloudRun:
    """Registry of deployed Cloud Run services with HTTP-request invocation."""

    def __init__(self):
        self._lock = threading.RLock()
        self._services: Dict[str, Service] = {}

    # ----- service lifecycle -----
    def deploy(self, name: str,
               handler: Callable[[dict], Any],
               *,
               region: str = "local",
               config: Optional[ServiceConfig] = None) -> Service:
        """Deploy (or redeploy) a service.

        ``handler`` receives a request dict and must return a response dict or
        None.  Redeploying an existing service replaces its handler and config.
        """
        with self._lock:
            if name in self._services:
                svc = self._services[name]
                svc._handler = handler
                if config is not None:
                    svc.config = config
                    svc._sem = threading.Semaphore(config.max_concurrency)
                return svc
            cfg = config or ServiceConfig()
            svc = Service(name=name, region=region, config=cfg, _handler=handler)
            self._services[name] = svc
            return svc

    def get_service(self, name: str) -> Service:
        with self._lock:
            if name not in self._services:
                raise ServiceNotFound(name)
            return self._services[name]

    def list_services(self) -> List[dict]:
        with self._lock:
            return [s.to_dict() for s in sorted(self._services.values(),
                                                  key=lambda s: s.name)]

    def delete_service(self, name: str) -> None:
        with self._lock:
            if name not in self._services:
                raise ServiceNotFound(name)
            del self._services[name]

    # ----- invocation -----
    def invoke(self, name: str, *,
               method: str = "POST",
               path: str = "/",
               headers: Optional[Dict[str, str]] = None,
               body: bytes = b"",
               query_params: Optional[Dict[str, List[str]]] = None) -> dict:
        """Invoke a deployed service and return the response dict."""
        with self._lock:
            if name not in self._services:
                raise ServiceNotFound(name)
            svc = self._services[name]
            if svc.state != "ACTIVE":
                raise CloudRunError(f"service {name!r} is not ACTIVE")
            handler = svc._handler

        acquired = svc._sem.acquire(timeout=svc.config.timeout)
        if not acquired:
            raise CloudRunError(f"concurrency limit reached for service {name!r}")

        request = {
            "method": method,
            "path": path,
            "headers": dict(headers or {}),
            "body": body,
            "queryParams": dict(query_params or {}),
        }
        t0 = time.time()
        try:
            raw = handler(request) if handler is not None else None
            response = _normalize_response(raw)
            latency = time.time() - t0
            record = InvocationRecord(timestamp=t0, method=method, path=path,
                                      status=response["status"], ok=True,
                                      latency=latency)
            with self._lock:
                svc.invocation_count += 1
                svc.last_status = response["status"]
                svc._log.append(record)
            return response
        except Exception as exc:
            latency = time.time() - t0
            err = f"{exc}\n{traceback.format_exc()}"
            record = InvocationRecord(timestamp=t0, method=method, path=path,
                                      status=500, ok=False, error=err,
                                      latency=latency)
            with self._lock:
                svc.invocation_count += 1
                svc.last_status = 500
                svc._log.append(record)
            return {"status": 500, "headers": {}, "body": err.encode("utf-8")}
        finally:
            svc._sem.release()

    def invocations(self, name: str) -> List[dict]:
        """Return the invocation log for a service."""
        with self._lock:
            if name not in self._services:
                raise ServiceNotFound(name)
            return [
                {"timestamp": r.timestamp, "method": r.method, "path": r.path,
                 "status": r.status, "ok": r.ok, "error": r.error,
                 "latency": r.latency}
                for r in self._services[name]._log
            ]


def _normalize_response(raw: Any) -> dict:
    """Convert a handler return value to a canonical response dict."""
    if raw is None:
        return {"status": 200, "headers": {}, "body": b""}
    if isinstance(raw, dict):
        status = raw.get("status", 200)
        headers = dict(raw.get("headers", {}))
        body = raw.get("body", b"")
        if isinstance(body, str):
            body = body.encode("utf-8")
        return {"status": status, "headers": headers, "body": body}
    if isinstance(raw, (bytes, str)):
        body = raw if isinstance(raw, bytes) else raw.encode("utf-8")
        return {"status": 200, "headers": {}, "body": body}
    return {"status": 200, "headers": {}, "body": str(raw).encode("utf-8")}
