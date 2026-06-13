"""Cloud-Functions-style event runner.

Implements a compatible SUBSET of the event-driven functions model:
register a Python callable as a "function" bound to an event trigger, then
fire events that invoke matching handlers. Four trigger types are supported:

  * ``object.finalize``     - fired when an object is written to storage.
  * ``pubsub.publish``      - fired when a message is published to a topic.
  * ``http``                - callable via an HTTP-style request dict.
  * ``firestore.write``     - fired when a Firestore document is created/updated/deleted.

Handlers receive an ``event`` dict (mirroring the CloudEvents-ish shape used by
GCP background functions) and may return a value, which is captured.

This is an independent reimplementation for LOCAL development. It is NOT
affiliated with or endorsed by Google.
"""

from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


class FunctionError(Exception):
    pass


OBJECT_FINALIZE = "object.finalize"
PUBSUB_PUBLISH = "pubsub.publish"
HTTP_TRIGGER = "http"
FIRESTORE_WRITE = "firestore.write"

_VALID_TRIGGERS = {OBJECT_FINALIZE, PUBSUB_PUBLISH, HTTP_TRIGGER, FIRESTORE_WRITE}


@dataclass
class Invocation:
    function: str
    event_type: str
    resource: str
    ok: bool
    result: Any = None
    error: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class _Function:
    name: str
    trigger: str
    handler: Callable[[dict], Any]
    resource: Optional[str]  # bucket name, topic name, collection; None = all


class FunctionRunner:
    """Registry of event handlers plus a synchronous dispatcher.

    Optionally wires itself to an :class:`ObjectStorage` and/or :class:`PubSub`
    so that real writes/publishes automatically dispatch events.
    """

    def __init__(self, storage=None, pubsub=None):
        self._lock = threading.RLock()
        self._functions: Dict[str, _Function] = {}
        self._log: List[Invocation] = []
        self._storage = storage
        self._pubsub = pubsub
        if pubsub is not None:
            pubsub.add_publish_hook(self._on_publish)

    # ----- registration -----
    def register(self, name: str, trigger: str,
                 handler: Callable[[dict], Any],
                 resource: Optional[str] = None) -> None:
        if trigger not in _VALID_TRIGGERS:
            raise FunctionError(f"unknown trigger: {trigger}")
        if not callable(handler):
            raise FunctionError("handler must be callable")
        with self._lock:
            self._functions[name] = _Function(name, trigger, handler, resource)

    def unregister(self, name: str) -> None:
        with self._lock:
            self._functions.pop(name, None)

    def list_functions(self) -> List[dict]:
        with self._lock:
            return [
                {"name": f.name, "trigger": f.trigger, "resource": f.resource}
                for f in self._functions.values()
            ]

    # ----- dispatch -----
    def _dispatch(self, trigger: str, resource: str, event: dict) -> List[Invocation]:
        with self._lock:
            matches = [f for f in self._functions.values()
                       if f.trigger == trigger
                       and (f.resource is None or f.resource == resource)]
        results = []
        for fn in matches:
            try:
                res = fn.handler(event)
                inv = Invocation(fn.name, trigger, resource, True, result=res)
            except Exception as exc:  # noqa: BLE001 - capture handler failures
                inv = Invocation(fn.name, trigger, resource, False,
                                 error=f"{exc}\n{traceback.format_exc()}")
            with self._lock:
                self._log.append(inv)
            results.append(inv)
        return results

    def fire_object_finalize(self, bucket: str, name: str,
                             size: int = 0, **extra) -> List[Invocation]:
        event = {
            "eventType": OBJECT_FINALIZE,
            "bucket": bucket,
            "name": name,
            "size": size,
            "timeCreated": time.time(),
        }
        event.update(extra)
        return self._dispatch(OBJECT_FINALIZE, bucket, event)

    def fire_pubsub_publish(self, topic: str, data, attributes=None) -> List[Invocation]:
        if isinstance(data, bytes):
            payload = data
        else:
            payload = str(data).encode("utf-8")
        event = {
            "eventType": PUBSUB_PUBLISH,
            "topic": topic,
            "data": payload,
            "attributes": dict(attributes or {}),
            "timeCreated": time.time(),
        }
        return self._dispatch(PUBSUB_PUBLISH, topic, event)

    def fire_http(self, function_name: str, request: dict) -> Optional[Invocation]:
        """Invoke a single named HTTP-triggered function with a request dict.

        ``request`` shape: {method, path, headers, body, queryParams}

        Returns the :class:`Invocation` record or None if the function is not
        registered with an ``http`` trigger.
        """
        with self._lock:
            fn = self._functions.get(function_name)
        if fn is None or fn.trigger != HTTP_TRIGGER:
            return None
        try:
            res = fn.handler(request)
            inv = Invocation(fn.name, HTTP_TRIGGER, function_name, True, result=res)
        except Exception as exc:
            inv = Invocation(fn.name, HTTP_TRIGGER, function_name, False,
                             error=f"{exc}\n{traceback.format_exc()}")
        with self._lock:
            self._log.append(inv)
        return inv

    def fire_firestore_write(self, collection: str, doc_id: str,
                             operation: str, data: dict,
                             old_data: Optional[dict] = None) -> List[Invocation]:
        """Fire a firestore.write event for a collection document change.

        ``operation`` is one of 'CREATE', 'UPDATE', 'DELETE'.
        ``resource`` matching uses the collection name.
        """
        event = {
            "eventType": FIRESTORE_WRITE,
            "collection": collection,
            "docId": doc_id,
            "operation": operation,
            "data": data,
            "oldData": old_data,
            "timeCreated": time.time(),
        }
        return self._dispatch(FIRESTORE_WRITE, collection, event)

    # hook used by PubSub.publish
    def _on_publish(self, topic: str, message) -> None:
        self.fire_pubsub_publish(topic, message.data, message.attributes)

    # ----- introspection -----
    def invocations(self, function: Optional[str] = None) -> List[Invocation]:
        with self._lock:
            if function is None:
                return list(self._log)
            return [i for i in self._log if i.function == function]

    def clear_log(self) -> None:
        with self._lock:
            self._log.clear()
