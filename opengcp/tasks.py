"""Cloud Tasks-style task queue service.

Implements a compatible SUBSET of the Cloud Tasks API:
  * Create/list/delete queues with configurable rate limits and retry settings.
  * Create tasks with optional scheduled dispatch time (``schedule_time``).
  * HTTP tasks: opengcp dispatches them by calling a registered handler callable
    or, for tasks with a ``url``, by POSTing to that URL over urllib.
  * Task introspection (list tasks in a queue, get task by name).

The dispatcher runs in a daemon thread and polls queues every 50 ms.

This is an independent reimplementation for LOCAL development. It is NOT
affiliated with or endorsed by Google.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


class TasksError(Exception):
    pass


class QueueNotFound(TasksError):
    pass


class TaskNotFound(TasksError):
    pass


@dataclass
class RetryConfig:
    max_attempts: int = 5
    min_backoff: float = 0.1   # seconds
    max_backoff: float = 3600.0
    max_doublings: int = 16


@dataclass
class RateLimits:
    max_dispatches_per_second: float = 500.0
    max_concurrent_dispatches: int = 1000


@dataclass
class Queue:
    name: str
    retry_config: RetryConfig = field(default_factory=RetryConfig)
    rate_limits: RateLimits = field(default_factory=RateLimits)
    state: str = "RUNNING"   # RUNNING | PAUSED

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "state": self.state,
            "retryConfig": {
                "maxAttempts": self.retry_config.max_attempts,
                "minBackoff": self.retry_config.min_backoff,
                "maxBackoff": self.retry_config.max_backoff,
                "maxDoublings": self.retry_config.max_doublings,
            },
            "rateLimits": {
                "maxDispatchesPerSecond": self.rate_limits.max_dispatches_per_second,
                "maxConcurrentDispatches": self.rate_limits.max_concurrent_dispatches,
            },
        }


@dataclass
class Task:
    name: str
    queue: str
    # HTTP-style task payload
    url: Optional[str] = None
    method: str = "POST"
    headers: Dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    # schedule: Unix timestamp; None = dispatch immediately
    schedule_time: Optional[float] = None
    # internal state
    state: str = "SCHEDULED"   # SCHEDULED | DISPATCHING | SUCCEEDED | FAILED
    create_time: float = field(default_factory=time.time)
    dispatch_count: int = 0
    last_attempt_time: Optional[float] = None
    response_status: Optional[int] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "queue": self.queue,
            "url": self.url,
            "method": self.method,
            "headers": dict(self.headers),
            "body": self.body.decode("utf-8", errors="replace"),
            "scheduleTime": self.schedule_time,
            "state": self.state,
            "createTime": self.create_time,
            "dispatchCount": self.dispatch_count,
            "lastAttemptTime": self.last_attempt_time,
            "responseStatus": self.response_status,
            "error": self.error,
        }


class CloudTasks:
    """Thread-safe Cloud Tasks emulator with a background dispatcher thread."""

    def __init__(self):
        self._lock = threading.RLock()
        self._queues: Dict[str, Queue] = {}
        self._tasks: Dict[str, Dict[str, Task]] = {}  # queue_name -> {task_name -> Task}
        # handler registry: queue_name -> callable(task_dict) -> None
        self._handlers: Dict[str, Callable[[dict], Any]] = {}
        self._stop_event = threading.Event()
        self._dispatcher = threading.Thread(target=self._dispatch_loop,
                                            daemon=True, name="cloudtasks-dispatcher")
        self._dispatcher.start()

    # ----- queue operations -----
    def create_queue(self, name: str, *,
                     retry_config: Optional[RetryConfig] = None,
                     rate_limits: Optional[RateLimits] = None) -> Queue:
        with self._lock:
            if name in self._queues:
                raise TasksError(f"queue exists: {name}")
            q = Queue(name=name,
                      retry_config=retry_config or RetryConfig(),
                      rate_limits=rate_limits or RateLimits())
            self._queues[name] = q
            self._tasks[name] = {}
            return q

    def get_queue(self, name: str) -> Queue:
        with self._lock:
            if name not in self._queues:
                raise QueueNotFound(name)
            return self._queues[name]

    def list_queues(self) -> List[dict]:
        with self._lock:
            return [q.to_dict() for q in sorted(self._queues.values(),
                                                  key=lambda q: q.name)]

    def delete_queue(self, name: str) -> None:
        with self._lock:
            if name not in self._queues:
                raise QueueNotFound(name)
            del self._queues[name]
            del self._tasks[name]

    def pause_queue(self, name: str) -> None:
        with self._lock:
            if name not in self._queues:
                raise QueueNotFound(name)
            self._queues[name].state = "PAUSED"

    def resume_queue(self, name: str) -> None:
        with self._lock:
            if name not in self._queues:
                raise QueueNotFound(name)
            self._queues[name].state = "RUNNING"

    def purge_queue(self, name: str) -> int:
        """Delete all tasks in the queue. Returns the number removed."""
        with self._lock:
            if name not in self._queues:
                raise QueueNotFound(name)
            n = len(self._tasks[name])
            self._tasks[name] = {}
            return n

    # ----- task operations -----
    def create_task(self, queue: str, *,
                    url: Optional[str] = None,
                    method: str = "POST",
                    headers: Optional[Dict[str, str]] = None,
                    body: bytes = b"",
                    schedule_time: Optional[float] = None,
                    name: Optional[str] = None) -> Task:
        with self._lock:
            if queue not in self._queues:
                raise QueueNotFound(queue)
            task_name = name or uuid.uuid4().hex
            if task_name in self._tasks[queue]:
                raise TasksError(f"task exists: {task_name}")
            t = Task(
                name=task_name,
                queue=queue,
                url=url,
                method=method,
                headers=dict(headers or {}),
                body=body,
                schedule_time=schedule_time,
                create_time=time.time(),
            )
            self._tasks[queue][task_name] = t
            return t

    def get_task(self, queue: str, task_name: str) -> Task:
        with self._lock:
            if queue not in self._queues:
                raise QueueNotFound(queue)
            t = self._tasks[queue].get(task_name)
            if t is None:
                raise TaskNotFound(task_name)
            return t

    def list_tasks(self, queue: str) -> List[dict]:
        with self._lock:
            if queue not in self._queues:
                raise QueueNotFound(queue)
            return [t.to_dict() for t in sorted(self._tasks[queue].values(),
                                                  key=lambda t: t.create_time)]

    def delete_task(self, queue: str, task_name: str) -> None:
        with self._lock:
            if queue not in self._queues:
                raise QueueNotFound(queue)
            if task_name not in self._tasks[queue]:
                raise TaskNotFound(task_name)
            del self._tasks[queue][task_name]

    # ----- handler registry -----
    def register_handler(self, queue: str, handler: Callable[[dict], Any]) -> None:
        """Register a callable to handle tasks dispatched from ``queue``.

        The handler receives the task's ``to_dict()`` payload and should not
        raise; exceptions are caught and the task is marked FAILED.
        """
        with self._lock:
            self._handlers[queue] = handler

    # ----- dispatcher -----
    def _dispatch_loop(self) -> None:
        while not self._stop_event.is_set():
            self._dispatch_due()
            time.sleep(0.05)

    def _dispatch_due(self) -> None:
        now = time.time()
        with self._lock:
            queues_snapshot = list(self._queues.items())
        for q_name, queue in queues_snapshot:
            if queue.state != "RUNNING":
                continue
            with self._lock:
                tasks_snapshot = list(self._tasks.get(q_name, {}).values())
            for task in tasks_snapshot:
                if task.state != "SCHEDULED":
                    continue
                if task.schedule_time is not None and task.schedule_time > now:
                    continue
                self._execute_task(task)

    def _execute_task(self, task: Task) -> None:
        with self._lock:
            # re-check state under lock
            q_tasks = self._tasks.get(task.queue, {})
            if task.name not in q_tasks or task.state != "SCHEDULED":
                return
            task.state = "DISPATCHING"
            task.dispatch_count += 1
            task.last_attempt_time = time.time()
            handler = self._handlers.get(task.queue)

        try:
            if handler is not None:
                handler(task.to_dict())
                status = 200
            elif task.url:
                status = self._http_dispatch(task)
            else:
                # no handler and no URL: succeed immediately
                status = 200
            with self._lock:
                task.state = "SUCCEEDED"
                task.response_status = status
        except Exception as exc:
            with self._lock:
                q = self._queues.get(task.queue)
                max_attempts = q.retry_config.max_attempts if q else 5
                if task.dispatch_count >= max_attempts:
                    task.state = "FAILED"
                    task.error = str(exc)
                else:
                    # exponential backoff then reschedule
                    backoff = min(
                        (q.retry_config.min_backoff if q else 0.1) *
                        (2 ** min(task.dispatch_count - 1,
                                  q.retry_config.max_doublings if q else 16)),
                        q.retry_config.max_backoff if q else 3600.0,
                    )
                    task.schedule_time = time.time() + backoff
                    task.state = "SCHEDULED"

    @staticmethod
    def _http_dispatch(task: Task) -> int:
        req = urllib.request.Request(
            task.url,
            data=task.body or None,
            method=task.method,
            headers=task.headers,
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status

    def stop(self) -> None:
        self._stop_event.set()
