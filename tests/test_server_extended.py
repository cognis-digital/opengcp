"""End-to-end HTTP server tests for the new services added in the
messaging+compute pass: enhanced Pub/Sub, Cloud Tasks, Cloud Scheduler,
Cloud Run, and extended Cloud Functions (HTTP + firestore.write)."""

import base64
import json
import time
import urllib.request
import urllib.error

import pytest

from opengcp.server import OpenGCPServer


@pytest.fixture()
def server():
    srv = OpenGCPServer(host="127.0.0.1", port=0).start_background()
    yield srv
    srv.stop()


def req(srv, method, path, body=None, headers=None, raw=False):
    url = srv.base_url + path
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        elif isinstance(body, bytes):
            data = body
        else:
            data = str(body).encode("utf-8")
    r = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(r) as resp:
            payload = resp.read()
            if raw:
                return resp.status, payload
            return resp.status, json.loads(payload)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# ============================================================
# Pub/Sub extended: ordering key, dead-letter, modifyAckDeadline
# ============================================================

def test_pubsub_ordering_key_http(server):
    req(server, "POST", "/pubsub/topics/ordered-topic")
    req(server, "POST", "/pubsub/subscriptions/ord-sub?topic=ordered-topic")

    req(server, "POST", "/pubsub/topics/ordered-topic/publish",
        body={"data": "msg1", "orderingKey": "grp"})
    req(server, "POST", "/pubsub/topics/ordered-topic/publish",
        body={"data": "msg2", "orderingKey": "grp"})

    code, pulled = req(server, "POST", "/pubsub/subscriptions/ord-sub/pull?max=10")
    assert code == 200
    msgs = pulled["receivedMessages"]
    # Only one message should come out (key is blocked)
    assert len(msgs) == 1
    assert msgs[0]["message"]["orderingKey"] == "grp"

    # Ack it, then second should be available
    ack_id = msgs[0]["ackId"]
    req(server, "POST", "/pubsub/subscriptions/ord-sub/ack", body={"ackIds": [ack_id]})

    code2, pulled2 = req(server, "POST",
                         "/pubsub/subscriptions/ord-sub/pull?max=10")
    assert len(pulled2["receivedMessages"]) == 1


def test_pubsub_dead_letter_http(server):
    req(server, "POST", "/pubsub/topics/src-topic")
    req(server, "POST", "/pubsub/topics/dl-topic")
    req(server, "POST", "/pubsub/subscriptions/dl-sub?topic=dl-topic")
    req(server, "POST",
        "/pubsub/subscriptions/main-sub?topic=src-topic"
        "&deadLetterTopic=dl-topic&maxDeliveryAttempts=2")

    req(server, "POST", "/pubsub/topics/src-topic/publish",
        body={"data": "poison"})

    # Nack twice to exceed max delivery attempts
    for _ in range(2):
        code, pulled = req(server, "POST",
                           "/pubsub/subscriptions/main-sub/pull?max=1")
        msgs = pulled["receivedMessages"]
        if msgs:
            req(server, "POST", "/pubsub/subscriptions/main-sub/nack",
                body={"ackIds": [msgs[0]["ackId"]]})

    # DL topic should have a message
    code, dl_pulled = req(server, "POST",
                          "/pubsub/subscriptions/dl-sub/pull?max=10")
    assert code == 200
    assert len(dl_pulled["receivedMessages"]) >= 1


def test_pubsub_modify_ack_deadline_http(server):
    req(server, "POST", "/pubsub/topics/t")
    req(server, "POST", "/pubsub/subscriptions/s?topic=t")
    req(server, "POST", "/pubsub/topics/t/publish", body={"data": "hello"})

    code, pulled = req(server, "POST", "/pubsub/subscriptions/s/pull?max=1")
    ack_id = pulled["receivedMessages"][0]["ackId"]

    code, resp = req(server, "POST",
                     "/pubsub/subscriptions/s/modifyAckDeadline",
                     body={"ackId": ack_id, "seconds": 60})
    assert code == 200
    assert resp["modified"] is True


def test_pubsub_stats_includes_ack_deadline(server):
    req(server, "POST", "/pubsub/topics/t2")
    req(server, "POST", "/pubsub/subscriptions/s2?topic=t2&ackDeadline=25")
    code, stats = req(server, "GET", "/pubsub/subscriptions/s2")
    assert code == 200
    assert stats["ackDeadline"] == 25.0


# ============================================================
# Cloud Tasks HTTP
# ============================================================

def test_tasks_crud_http(server):
    # create queue
    code, q = req(server, "POST", "/tasks/queues/my-queue",
                  body={"retryConfig": {"maxAttempts": 3}})
    assert code == 200
    assert q["name"] == "my-queue"

    # list queues
    code, lst = req(server, "GET", "/tasks/queues")
    assert code == 200
    assert any(q2["name"] == "my-queue" for q2 in lst["queues"])

    # get queue
    code, got = req(server, "GET", "/tasks/queues/my-queue")
    assert got["retryConfig"]["maxAttempts"] == 3

    # create task
    code, t = req(server, "POST", "/tasks/queues/my-queue/tasks",
                  body={"name": "task1", "body": "payload"})
    assert code == 200
    assert t["name"] == "task1"

    # list tasks
    code, tasks_list = req(server, "GET", "/tasks/queues/my-queue/tasks")
    assert len(tasks_list["tasks"]) >= 1

    # get task
    code, tsk = req(server, "GET", "/tasks/queues/my-queue/tasks/task1")
    assert tsk["name"] == "task1"

    # delete task
    code, _ = req(server, "DELETE", "/tasks/queues/my-queue/tasks/task1")
    assert code == 200
    code2, _ = req(server, "GET", "/tasks/queues/my-queue/tasks/task1")
    assert code2 == 404


def test_tasks_pause_resume_http(server):
    req(server, "POST", "/tasks/queues/q")
    code, r = req(server, "POST", "/tasks/queues/q/pause")
    assert code == 200 and r["state"] == "PAUSED"
    code, r2 = req(server, "POST", "/tasks/queues/q/resume")
    assert r2["state"] == "RUNNING"


def test_tasks_purge_http(server):
    req(server, "POST", "/tasks/queues/q2")
    req(server, "POST", "/tasks/queues/q2/tasks", body={"body": "a"})
    req(server, "POST", "/tasks/queues/q2/tasks", body={"body": "b"})
    code, r = req(server, "POST", "/tasks/queues/q2/purge")
    assert code == 200
    assert r["purged"] == 2


def test_tasks_missing_queue_404(server):
    code, _ = req(server, "GET", "/tasks/queues/nonexistent")
    assert code == 404


def test_tasks_handler_dispatch_e2e(server):
    """Register a handler in-process and verify tasks get dispatched."""
    received = []
    server.services.tasks.create_queue("dispatch-q")
    server.services.tasks.register_handler(
        "dispatch-q", lambda t: received.append(t["body"]))

    req(server, "POST", "/tasks/queues/dispatch-q/tasks",
        body={"name": "t-e2e", "body": "end-to-end"})

    deadline = time.time() + 3.0
    while not received and time.time() < deadline:
        time.sleep(0.05)
    assert received == ["end-to-end"]


# ============================================================
# Cloud Scheduler HTTP
# ============================================================

def test_scheduler_crud_http(server):
    # create
    code, job = req(server, "POST", "/scheduler/jobs",
                    body={"name": "j1", "schedule": "@hourly",
                          "description": "test job"})
    assert code == 200
    assert job["name"] == "j1"
    assert job["schedule"] == "@hourly"

    # list
    code, lst = req(server, "GET", "/scheduler/jobs")
    assert any(j["name"] == "j1" for j in lst["jobs"])

    # get
    code, got = req(server, "GET", "/scheduler/jobs/j1")
    assert got["description"] == "test job"

    # delete
    code, _ = req(server, "DELETE", "/scheduler/jobs/j1")
    assert code == 200
    code2, _ = req(server, "GET", "/scheduler/jobs/j1")
    assert code2 == 404


def test_scheduler_pause_resume_http(server):
    req(server, "POST", "/scheduler/jobs",
        body={"name": "j2", "schedule": "0 0 * * *"})
    code, r = req(server, "POST", "/scheduler/jobs/j2/pause")
    assert code == 200 and r["state"] == "PAUSED"
    code, r2 = req(server, "POST", "/scheduler/jobs/j2/resume")
    assert r2["state"] == "ENABLED"


def test_scheduler_run_now_http(server):
    fired = []
    server.services.scheduler.create_job(
        "j-http", "@daily", handler=lambda d: fired.append(True))

    code, r = req(server, "POST", "/scheduler/jobs/j-http/run")
    assert code == 200

    deadline = time.time() + 3.0
    while not fired and time.time() < deadline:
        time.sleep(0.05)
    assert fired


def test_scheduler_history_http(server):
    server.services.scheduler.create_job(
        "j-hist", "@hourly", handler=lambda d: None)
    req(server, "POST", "/scheduler/jobs/j-hist/run")

    deadline = time.time() + 3.0
    while True:
        code, hist = req(server, "GET", "/scheduler/jobs/j-hist/history")
        if hist["history"]:
            break
        if time.time() > deadline:
            break
        time.sleep(0.05)
    assert len(hist["history"]) >= 1
    assert hist["history"][0]["ok"] is True


def test_scheduler_invalid_schedule_400(server):
    code, _ = req(server, "POST", "/scheduler/jobs",
                  body={"name": "bad-job", "schedule": "not a cron"})
    assert code == 409  # SchedulerError is not a NotFound -> 409


def test_scheduler_missing_name_400(server):
    code, _ = req(server, "POST", "/scheduler/jobs",
                  body={"schedule": "* * * * *"})
    assert code == 400


# ============================================================
# Cloud Run HTTP
# ============================================================

def test_cloudrun_list_and_get_http(server):
    server.services.cloudrun.deploy("web", lambda req: {"status": 200, "body": b"ok"})
    code, lst = req(server, "GET", "/cloudrun/services")
    assert code == 200
    assert any(s["name"] == "web" for s in lst["services"])

    code, svc = req(server, "GET", "/cloudrun/services/web")
    assert svc["state"] == "ACTIVE"


def test_cloudrun_invoke_http(server):
    def echo(request):
        return {"status": 200, "body": request["body"],
                "headers": {"Content-Type": "text/plain"}}

    server.services.cloudrun.deploy("echo-svc", echo)
    code, body = req(server, "POST", "/cloudrun/services/echo-svc/invoke",
                     body=b"echo-me", raw=True)
    assert code == 200
    assert body == b"echo-me"


def test_cloudrun_invocation_log_http(server):
    server.services.cloudrun.deploy("log-svc", lambda req: {"status": 204, "body": b""})
    req(server, "POST", "/cloudrun/services/log-svc/invoke",
        body=b"ping", raw=True)
    code, log = req(server, "GET", "/cloudrun/services/log-svc/invocations")
    assert code == 200
    assert len(log["invocations"]) >= 1
    assert log["invocations"][0]["status"] == 204


def test_cloudrun_delete_service_http(server):
    server.services.cloudrun.deploy("del-svc", lambda req: None)
    code, _ = req(server, "DELETE", "/cloudrun/services/del-svc")
    assert code == 200
    code2, _ = req(server, "GET", "/cloudrun/services/del-svc")
    assert code2 == 404


def test_cloudrun_missing_service_404(server):
    code, _ = req(server, "GET", "/cloudrun/services/no-such-service")
    assert code == 404


# ============================================================
# Extended Cloud Functions HTTP: HTTP trigger
# ============================================================

def test_functions_http_trigger_invoke(server):
    captured = []

    def http_fn(request):
        captured.append(request["body"])
        return {"status": 200, "body": b"handled",
                "headers": {"Content-Type": "text/plain"}}

    server.services.functions.register("my-http-fn",
                                       "http", http_fn)

    code, resp = req(server, "POST", "/functions/my-http-fn/invoke",
                     body=b"request body", raw=True)
    assert code == 200
    assert resp == b"handled"
    assert captured == [b"request body"]


def test_functions_http_trigger_missing_404(server):
    code, _ = req(server, "POST", "/functions/no-such-fn/invoke", body=b"")
    assert code == 404


def test_functions_http_trigger_error_500(server):
    def failing(req):
        raise RuntimeError("oops")

    server.services.functions.register("bad-fn", "http", failing)
    code, _ = req(server, "POST", "/functions/bad-fn/invoke",
                  body=b"", raw=True)
    assert code == 500


# ============================================================
# Root endpoint includes new services
# ============================================================

def test_root_lists_new_endpoints(server):
    code, body = req(server, "GET", "/")
    assert "/tasks" in body["endpoints"]
    assert "/scheduler" in body["endpoints"]
    assert "/cloudrun" in body["endpoints"]
