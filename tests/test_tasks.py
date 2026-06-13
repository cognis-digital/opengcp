"""Tests for Cloud Tasks emulator (opengcp.tasks)."""

import time

import pytest

from opengcp.tasks import (CloudTasks, Queue, RetryConfig, RateLimits,
                           QueueNotFound, TaskNotFound, TasksError)


# ----- queue operations -----

def test_create_and_list_queues():
    ct = CloudTasks()
    ct.create_queue("q1")
    ct.create_queue("q2")
    queues = ct.list_queues()
    assert [q["name"] for q in queues] == ["q1", "q2"]
    ct.stop()


def test_create_queue_duplicate_raises():
    ct = CloudTasks()
    ct.create_queue("q")
    with pytest.raises(TasksError):
        ct.create_queue("q")
    ct.stop()


def test_get_queue():
    ct = CloudTasks()
    ct.create_queue("q", retry_config=RetryConfig(max_attempts=3),
                    rate_limits=RateLimits(max_dispatches_per_second=10.0))
    q = ct.get_queue("q")
    assert q.name == "q"
    d = q.to_dict()
    assert d["retryConfig"]["maxAttempts"] == 3
    assert d["rateLimits"]["maxDispatchesPerSecond"] == 10.0
    ct.stop()


def test_get_queue_not_found():
    ct = CloudTasks()
    with pytest.raises(QueueNotFound):
        ct.get_queue("nope")
    ct.stop()


def test_delete_queue():
    ct = CloudTasks()
    ct.create_queue("q")
    ct.delete_queue("q")
    with pytest.raises(QueueNotFound):
        ct.get_queue("q")
    ct.stop()


def test_pause_resume_queue():
    ct = CloudTasks()
    ct.create_queue("q")
    ct.pause_queue("q")
    assert ct.get_queue("q").state == "PAUSED"
    ct.resume_queue("q")
    assert ct.get_queue("q").state == "RUNNING"
    ct.stop()


def test_purge_queue():
    ct = CloudTasks()
    ct.create_queue("q")
    ct.create_task("q", body=b"a")
    ct.create_task("q", body=b"b")
    n = ct.purge_queue("q")
    assert n == 2
    assert ct.list_tasks("q") == []
    ct.stop()


# ----- task operations -----

def test_create_and_list_tasks():
    ct = CloudTasks()
    ct.create_queue("q")
    t = ct.create_task("q", body=b"hello", name="task1")
    assert t.name == "task1"
    tasks = ct.list_tasks("q")
    assert len(tasks) == 1
    assert tasks[0]["name"] == "task1"
    ct.stop()


def test_create_task_in_missing_queue():
    ct = CloudTasks()
    with pytest.raises(QueueNotFound):
        ct.create_task("missing", body=b"x")
    ct.stop()


def test_create_task_duplicate_name():
    ct = CloudTasks()
    ct.create_queue("q")
    ct.create_task("q", name="t1", body=b"x")
    with pytest.raises(TasksError):
        ct.create_task("q", name="t1", body=b"y")
    ct.stop()


def test_get_task():
    ct = CloudTasks()
    ct.create_queue("q")
    ct.create_task("q", name="t", body=b"data")
    task = ct.get_task("q", "t")
    assert task.body == b"data"
    ct.stop()


def test_get_task_not_found():
    ct = CloudTasks()
    ct.create_queue("q")
    with pytest.raises(TaskNotFound):
        ct.get_task("q", "nope")
    ct.stop()


def test_delete_task():
    ct = CloudTasks()
    ct.create_queue("q")
    ct.create_task("q", name="t", body=b"x")
    ct.delete_task("q", "t")
    with pytest.raises(TaskNotFound):
        ct.get_task("q", "t")
    ct.stop()


# ----- dispatch with registered handler -----

def test_handler_called_on_dispatch():
    ct = CloudTasks()
    ct.create_queue("q")
    received = []
    ct.register_handler("q", lambda task: received.append(task["body"]))
    ct.create_task("q", body=b"payload")

    deadline = time.time() + 3.0
    while not received and time.time() < deadline:
        time.sleep(0.05)

    assert received == ["payload"]  # body decoded as str in to_dict
    ct.stop()


def test_task_state_becomes_succeeded():
    ct = CloudTasks()
    ct.create_queue("q")
    ct.register_handler("q", lambda t: None)
    task = ct.create_task("q", name="t1", body=b"x")

    deadline = time.time() + 3.0
    while task.state not in ("SUCCEEDED", "FAILED") and time.time() < deadline:
        time.sleep(0.05)

    assert task.state == "SUCCEEDED"
    ct.stop()


def test_task_retries_on_handler_failure():
    ct = CloudTasks()
    ct.create_queue("q", retry_config=RetryConfig(max_attempts=2,
                                                   min_backoff=0.05,
                                                   max_backoff=0.1))
    call_count = [0]

    def failing(t):
        call_count[0] += 1
        raise RuntimeError("fail")

    ct.register_handler("q", failing)
    task = ct.create_task("q", name="ft", body=b"x")

    deadline = time.time() + 5.0
    while task.state != "FAILED" and time.time() < deadline:
        time.sleep(0.1)

    assert task.state == "FAILED"
    assert call_count[0] >= 2
    ct.stop()


def test_scheduled_task_dispatched_after_delay():
    ct = CloudTasks()
    ct.create_queue("q")
    received = []
    ct.register_handler("q", lambda t: received.append(True))

    # Schedule 0.3s in the future
    schedule = time.time() + 0.3
    ct.create_task("q", body=b"sched", schedule_time=schedule)

    # Not yet dispatched
    time.sleep(0.1)
    assert received == []

    # Now it should fire
    deadline = time.time() + 3.0
    while not received and time.time() < deadline:
        time.sleep(0.05)
    assert received == [True]
    ct.stop()


def test_paused_queue_does_not_dispatch():
    ct = CloudTasks()
    ct.create_queue("q")
    received = []
    ct.register_handler("q", lambda t: received.append(True))
    ct.pause_queue("q")
    ct.create_task("q", body=b"x")

    time.sleep(0.3)
    assert received == []
    ct.stop()


def test_task_to_dict_shape():
    ct = CloudTasks()
    ct.create_queue("q")
    task = ct.create_task("q", url="http://example.com/hook",
                          method="POST", headers={"X-Custom": "val"},
                          body=b"data", schedule_time=None, name="myTask")
    d = task.to_dict()
    assert d["name"] == "myTask"
    assert d["url"] == "http://example.com/hook"
    assert d["headers"]["X-Custom"] == "val"
    ct.stop()
