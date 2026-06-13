"""Cloud Scheduler-style cron-job service.

Implements a compatible SUBSET of the Cloud Scheduler API:
  * Create/list/get/delete/pause/resume jobs.
  * Each job has a ``schedule`` (standard 5- or 6-field cron expression) and a
    registered Python handler callable that receives a job-description dict.
  * A background thread evaluates all ENABLED jobs every second; when the next
    scheduled time is reached the handler is called in a worker thread.
  * ``run_now`` forces immediate dispatch (ignores the cron schedule).
  * ``last_attempt_time``, ``last_attempt_status``, and execution history are
    tracked on each job.

Cron expression support:
  ``* * * * *``  — minute, hour, day-of-month, month, day-of-week
  ``*/N``        — every-N step notation
  Number lists   — ``1,2,3``
  Ranges         — ``1-5``
  Mixed          — ``1-5,10,*/15``
  Aliases        — ``@hourly`` ``@daily`` ``@midnight`` ``@weekly`` ``@monthly`` ``@yearly`` ``@annually``

This is an independent reimplementation for LOCAL development. It is NOT
affiliated with or endorsed by Google.
"""

from __future__ import annotations

import re
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
import datetime


class SchedulerError(Exception):
    pass


class JobNotFound(SchedulerError):
    pass


# ----- cron parser -----

_ALIASES = {
    "@yearly":    "0 0 1 1 *",
    "@annually":  "0 0 1 1 *",
    "@monthly":   "0 0 1 * *",
    "@weekly":    "0 0 * * 0",
    "@daily":     "0 0 * * *",
    "@midnight":  "0 0 * * *",
    "@hourly":    "0 * * * *",
}

_FIELD_RANGES = [
    (0, 59),   # minute
    (0, 23),   # hour
    (1, 31),   # day of month
    (1, 12),   # month
    (0, 7),    # day of week (0 and 7 both = Sunday)
]


def _parse_field(token: str, lo: int, hi: int) -> set:
    """Parse a single cron field token into a set of ints."""
    result = set()
    for part in token.split(","):
        if part == "*":
            result.update(range(lo, hi + 1))
        elif part.startswith("*/"):
            step = int(part[2:])
            result.update(range(lo, hi + 1, step))
        elif "-" in part and "/" in part:
            range_part, _, step_part = part.partition("/")
            a, _, b = range_part.partition("-")
            result.update(range(int(a), int(b) + 1, int(step_part)))
        elif "-" in part:
            a, _, b = part.partition("-")
            result.update(range(int(a), int(b) + 1))
        else:
            result.add(int(part))
    # clamp day-of-week: 7 -> 0 (both mean Sunday)
    if 7 in result and hi == 7:
        result.discard(7)
        result.add(0)
    return result


def parse_cron(expression: str):
    """Return a 5-tuple of sets: (minutes, hours, days, months, weekdays)."""
    expr = _ALIASES.get(expression.strip(), expression.strip())
    parts = expr.split()
    if len(parts) != 5:
        raise SchedulerError(
            f"invalid cron expression (need 5 fields): {expression!r}")
    return tuple(
        _parse_field(parts[i], *_FIELD_RANGES[i]) for i in range(5)
    )


def cron_matches(cron_tuple, dt: datetime.datetime) -> bool:
    """Return True if the datetime matches the parsed cron tuple."""
    minutes, hours, days, months, weekdays = cron_tuple
    return (dt.minute in minutes and
            dt.hour in hours and
            dt.day in days and
            dt.month in months and
            dt.weekday() in {(w - 1) % 7 for w in weekdays}
            if weekdays != {0, 1, 2, 3, 4, 5, 6} else True)


def _cron_matches_simple(cron_tuple, dt: datetime.datetime) -> bool:
    """Match cron to a datetime without weekday aliasing complexity."""
    minutes, hours, days, months, weekdays = cron_tuple
    # Python weekday(): Mon=0 .. Sun=6
    # Cron weekday: Sun=0, Mon=1 .. Sat=6  (we stored 0-6 already)
    py_dow = dt.weekday()  # Mon=0..Sun=6
    cron_dow = (py_dow + 1) % 7  # Mon=1..Sun=0
    return (dt.minute in minutes and
            dt.hour in hours and
            dt.day in days and
            dt.month in months and
            cron_dow in weekdays)


@dataclass
class AttemptResult:
    timestamp: float
    ok: bool
    error: Optional[str] = None


@dataclass
class Job:
    name: str
    schedule: str
    description: str = ""
    state: str = "ENABLED"   # ENABLED | PAUSED | DISABLED
    create_time: float = field(default_factory=time.time)
    last_attempt_time: Optional[float] = None
    last_attempt_status: Optional[str] = None
    _cron: Any = field(default=None, repr=False)
    _history: List[AttemptResult] = field(default_factory=list, repr=False)
    # internal: wall-clock minute of last dispatch (avoids double-fire)
    _last_dispatched_minute: Optional[int] = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "schedule": self.schedule,
            "description": self.description,
            "state": self.state,
            "createTime": self.create_time,
            "lastAttemptTime": self.last_attempt_time,
            "lastAttemptStatus": self.last_attempt_status,
        }


class CloudScheduler:
    """Thread-safe Cloud Scheduler emulator with a background evaluator thread."""

    def __init__(self):
        self._lock = threading.RLock()
        self._jobs: Dict[str, Job] = {}
        self._handlers: Dict[str, Callable[[dict], Any]] = {}
        self._stop_event = threading.Event()
        self._evaluator = threading.Thread(target=self._evaluate_loop,
                                           daemon=True, name="scheduler-evaluator")
        self._evaluator.start()

    # ----- job operations -----
    def create_job(self, name: str, schedule: str, *,
                   description: str = "",
                   handler: Optional[Callable[[dict], Any]] = None) -> Job:
        cron = parse_cron(schedule)   # validate early
        with self._lock:
            if name in self._jobs:
                raise SchedulerError(f"job exists: {name}")
            job = Job(name=name, schedule=schedule, description=description,
                      _cron=cron)
            self._jobs[name] = job
            if handler is not None:
                self._handlers[name] = handler
        return job

    def get_job(self, name: str) -> Job:
        with self._lock:
            if name not in self._jobs:
                raise JobNotFound(name)
            return self._jobs[name]

    def list_jobs(self) -> List[dict]:
        with self._lock:
            return [j.to_dict() for j in sorted(self._jobs.values(),
                                                  key=lambda j: j.name)]

    def delete_job(self, name: str) -> None:
        with self._lock:
            if name not in self._jobs:
                raise JobNotFound(name)
            del self._jobs[name]
            self._handlers.pop(name, None)

    def pause_job(self, name: str) -> None:
        with self._lock:
            if name not in self._jobs:
                raise JobNotFound(name)
            self._jobs[name].state = "PAUSED"

    def resume_job(self, name: str) -> None:
        with self._lock:
            if name not in self._jobs:
                raise JobNotFound(name)
            self._jobs[name].state = "ENABLED"

    def register_handler(self, name: str, handler: Callable[[dict], Any]) -> None:
        """Set or replace the handler for a job."""
        with self._lock:
            if name not in self._jobs:
                raise JobNotFound(name)
            self._handlers[name] = handler

    def run_now(self, name: str) -> None:
        """Force-dispatch a job immediately regardless of schedule."""
        with self._lock:
            if name not in self._jobs:
                raise JobNotFound(name)
            job = self._jobs[name]
            handler = self._handlers.get(name)
        self._dispatch_job(job, handler)

    def job_history(self, name: str) -> List[dict]:
        with self._lock:
            if name not in self._jobs:
                raise JobNotFound(name)
            return [{"timestamp": a.timestamp, "ok": a.ok, "error": a.error}
                    for a in self._jobs[name]._history]

    # ----- evaluator -----
    def _evaluate_loop(self) -> None:
        while not self._stop_event.is_set():
            now = datetime.datetime.now()
            # Only evaluate at second 0 of each minute to avoid multi-firing
            if now.second == 0:
                self._evaluate_minute(now)
                time.sleep(1.1)
            else:
                time.sleep(0.2)

    def _evaluate_minute(self, now: datetime.datetime) -> None:
        minute_key = now.year * 100000 + now.month * 10000 + now.day * 1000 + \
                     now.hour * 100 + now.minute
        with self._lock:
            jobs_snapshot = list(self._jobs.values())
        for job in jobs_snapshot:
            if job.state != "ENABLED":
                continue
            if job._last_dispatched_minute == minute_key:
                continue
            if not _cron_matches_simple(job._cron, now):
                continue
            with self._lock:
                job._last_dispatched_minute = minute_key
                handler = self._handlers.get(job.name)
            t = threading.Thread(target=self._dispatch_job, args=(job, handler),
                                 daemon=True)
            t.start()

    def _dispatch_job(self, job: Job, handler: Optional[Callable]) -> None:
        now = time.time()
        try:
            if handler is not None:
                handler(job.to_dict())
            result = AttemptResult(timestamp=now, ok=True)
            with self._lock:
                job.last_attempt_time = now
                job.last_attempt_status = "SUCCESS"
                job._history.append(result)
        except Exception as exc:
            err = f"{exc}\n{traceback.format_exc()}"
            result = AttemptResult(timestamp=now, ok=False, error=err)
            with self._lock:
                job.last_attempt_time = now
                job.last_attempt_status = "FAILED"
                job._history.append(result)

    def stop(self) -> None:
        self._stop_event.set()
