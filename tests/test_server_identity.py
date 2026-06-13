"""End-to-end HTTP server tests for the identity+security+ops services."""

from __future__ import annotations

import base64
import json
import threading
import time
import urllib.request
from urllib.error import HTTPError

import pytest

from opengcp.server import OpenGCPServer, Services


# ---- shared server fixture ----

@pytest.fixture(scope="module")
def server():
    svc = Services()
    srv = OpenGCPServer(host="127.0.0.1", port=0, services=svc)
    srv.start_background()
    yield srv
    srv.stop()


def _url(server, path):
    return f"{server.base_url}{path}"


def _post(server, path, data=None, content_type="application/json"):
    body = json.dumps(data).encode() if data is not None else b""
    req = urllib.request.Request(
        _url(server, path),
        data=body,
        headers={"Content-Type": content_type},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def _get(server, path):
    with urllib.request.urlopen(_url(server, path)) as resp:
        return json.loads(resp.read())


def _patch(server, path, data):
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        _url(server, path),
        data=body,
        headers={"Content-Type": "application/json"},
        method="PATCH",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def _delete(server, path):
    req = urllib.request.Request(_url(server, path), method="DELETE")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def _expect_error(server, method, path, data=None):
    body = json.dumps(data).encode() if data is not None else b""
    req = urllib.request.Request(
        _url(server, path),
        data=body if body else None,
        headers={"Content-Type": "application/json"} if body else {},
        method=method,
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except HTTPError as e:
        return e.code, json.loads(e.read())


# ============================================================
# IAM
# ============================================================

def test_iam_predefined_roles_listed(server):
    resp = _get(server, "/iam/roles")
    names = {r["name"] for r in resp["roles"]}
    assert "roles/owner" in names


def test_iam_create_get_delete_role(server):
    _post(server, "/iam/roles/roles/e2e.custom", {
        "title": "E2E Custom",
        "permissions": ["e2e.stuff.read"],
    })
    role = _get(server, "/iam/roles/roles/e2e.custom")
    assert role["name"] == "roles/e2e.custom"
    assert "e2e.stuff.read" in role["permissions"]
    _delete(server, "/iam/roles/roles/e2e.custom")
    code, _ = _expect_error(server, "GET", "/iam/roles/roles/e2e.custom")
    assert code == 404


def test_iam_patch_role(server):
    _post(server, "/iam/roles/roles/patch.me2", {
        "title": "Old Title",
        "permissions": ["x.y"],
    })
    _patch(server, "/iam/roles/roles/patch.me2", {"title": "New Title"})
    role = _get(server, "/iam/roles/roles/patch.me2")
    assert role["title"] == "New Title"


def test_iam_set_and_get_policy(server):
    bindings = [{"role": "roles/viewer",
                 "members": ["user:e2e@example.com"]}]
    resp = _post(server, "/iam/projects/e2e-project/policy", {"bindings": bindings})
    assert resp["bindings"][0]["role"] == "roles/viewer"
    resp2 = _get(server, "/iam/projects/e2e-project/policy")
    assert resp2["bindings"][0]["role"] == "roles/viewer"


def test_iam_test_permissions(server):
    _post(server, "/iam/projects/perm-test/policy", {
        "bindings": [{"role": "roles/editor",
                      "members": ["user:editor@example.com"]}]
    })
    resp = _post(server, "/iam/projects/perm-test/testPermissions", {
        "principal": "user:editor@example.com",
        "permissions": ["storage.objects.create", "storage.buckets.delete"],
    })
    assert "storage.objects.create" in resp["permissions"]
    assert "storage.buckets.delete" not in resp["permissions"]


def test_iam_register_and_list_resources(server):
    _post(server, "/iam/resources/projects/my-project?type=project")
    resp = _get(server, "/iam/resources")
    names = [r["name"] for r in resp["resources"]]
    assert "projects/my-project" in names


# ============================================================
# Secret Manager
# ============================================================

def test_secretmanager_create_and_get(server):
    resp = _post(server, "/secretmanager/secrets/e2e-secret",
                 {"labels": {"env": "test"}})
    assert resp["secretId"] == "e2e-secret"
    resp2 = _get(server, "/secretmanager/secrets/e2e-secret")
    assert resp2["secretId"] == "e2e-secret"


def test_secretmanager_list(server):
    _post(server, "/secretmanager/secrets/list-secret-a", {})
    resp = _get(server, "/secretmanager/secrets")
    ids = [s["secretId"] for s in resp["secrets"]]
    assert "list-secret-a" in ids


def test_secretmanager_add_and_access_version(server):
    _post(server, "/secretmanager/secrets/ver-secret", {})
    payload = b"super secret value"
    req = urllib.request.Request(
        _url(server, "/secretmanager/secrets/ver-secret/versions"),
        data=payload,
        headers={"Content-Type": "application/octet-stream"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        ver = json.loads(resp.read())
    assert ver["version"] == 1

    resp = _get(server, "/secretmanager/secrets/ver-secret/versions/1:access")
    decoded = base64.b64decode(resp["payload"])
    assert decoded == payload


def test_secretmanager_disable_and_enable_version(server):
    _post(server, "/secretmanager/secrets/dis-secret", {})
    req = urllib.request.Request(
        _url(server, "/secretmanager/secrets/dis-secret/versions"),
        data=b"data",
        headers={"Content-Type": "application/octet-stream"},
        method="POST",
    )
    urllib.request.urlopen(req)
    _post(server, "/secretmanager/secrets/dis-secret/versions/1:disable")
    code, _ = _expect_error(
        server, "GET",
        "/secretmanager/secrets/dis-secret/versions/1:access")
    assert code in (409, 500)
    _post(server, "/secretmanager/secrets/dis-secret/versions/1:enable")
    resp = _get(server, "/secretmanager/secrets/dis-secret/versions/1:access")
    assert base64.b64decode(resp["payload"]) == b"data"


def test_secretmanager_destroy_version(server):
    _post(server, "/secretmanager/secrets/dest-secret", {})
    req = urllib.request.Request(
        _url(server, "/secretmanager/secrets/dest-secret/versions"),
        data=b"x",
        method="POST",
    )
    urllib.request.urlopen(req)
    _post(server, "/secretmanager/secrets/dest-secret/versions/1:destroy")
    ver = _get(server, "/secretmanager/secrets/dest-secret/versions/1")
    assert ver["state"] == "DESTROYED"


def test_secretmanager_delete_secret(server):
    _post(server, "/secretmanager/secrets/todel-secret", {})
    _delete(server, "/secretmanager/secrets/todel-secret")
    code, _ = _expect_error(server, "GET", "/secretmanager/secrets/todel-secret")
    assert code == 404


# ============================================================
# Cloud KMS
# ============================================================

def test_kms_create_key_ring(server):
    resp = _post(server, "/kms/keyrings/e2e-ring")
    assert resp["name"] == "e2e-ring"


def test_kms_list_key_rings(server):
    _post(server, "/kms/keyrings/e2e-ring-list")
    resp = _get(server, "/kms/keyrings")
    names = [r["name"] for r in resp["keyRings"]]
    assert "e2e-ring-list" in names


def test_kms_create_and_get_key(server):
    _post(server, "/kms/keyrings/kr-for-key")
    resp = _post(server, "/kms/keyrings/kr-for-key/keys/my-key")
    assert resp["keyId"] == "my-key"
    resp2 = _get(server, "/kms/keyrings/kr-for-key/keys/my-key")
    assert resp2["keyId"] == "my-key"


def test_kms_list_keys(server):
    _post(server, "/kms/keyrings/kr-list-keys")
    _post(server, "/kms/keyrings/kr-list-keys/keys/k1")
    _post(server, "/kms/keyrings/kr-list-keys/keys/k2")
    resp = _get(server, "/kms/keyrings/kr-list-keys/keys")
    ids = [k["keyId"] for k in resp["cryptoKeys"]]
    assert "k1" in ids and "k2" in ids


def test_kms_encrypt_decrypt(server):
    _post(server, "/kms/keyrings/kr-enc")
    _post(server, "/kms/keyrings/kr-enc/keys/enc-key")
    plaintext_b64 = base64.b64encode(b"hello kms").decode()
    enc = _post(server, "/kms/keyrings/kr-enc/keys/enc-key/encrypt",
                {"plaintext": plaintext_b64})
    assert "ciphertext" in enc
    dec = _post(server, "/kms/keyrings/kr-enc/keys/enc-key/decrypt",
                {"ciphertext": enc["ciphertext"]})
    assert base64.b64decode(dec["plaintext"]) == b"hello kms"


def test_kms_generate_data_key(server):
    _post(server, "/kms/keyrings/kr-dek")
    _post(server, "/kms/keyrings/kr-dek/keys/dek-key")
    resp = _post(server, "/kms/keyrings/kr-dek/keys/dek-key/generateDataKey")
    assert "plaintext" in resp
    assert "ciphertextBlob" in resp
    dek = base64.b64decode(resp["plaintext"])
    assert len(dek) == 32


def test_kms_list_key_versions(server):
    _post(server, "/kms/keyrings/kr-versions")
    _post(server, "/kms/keyrings/kr-versions/keys/v-key")
    resp = _get(server, "/kms/keyrings/kr-versions/keys/v-key/versions")
    assert len(resp["cryptoKeyVersions"]) == 1


# ============================================================
# Cloud Logging
# ============================================================

def test_logging_write_and_list(server):
    _post(server, "/logging/entries:write", {
        "logName": "projects/local/logs/e2e-app",
        "entries": [
            {"severity": "INFO", "jsonPayload": {"msg": "test"}},
        ],
    })
    resp = _get(server,
                "/logging/entries?logName=projects/local/logs/e2e-app")
    assert len(resp["entries"]) >= 1


def test_logging_write_via_post(server):
    _post(server, "/logging/entries", {
        "logName": "projects/local/logs/e2e-post",
        "entries": [{"severity": "DEBUG", "jsonPayload": {"k": "v"}}],
    })
    resp = _get(server, "/logging/entries?logName=projects/local/logs/e2e-post")
    assert resp["entries"][0]["logName"] == "projects/local/logs/e2e-post"


def test_logging_filter_severity(server):
    _post(server, "/logging/entries:write", {
        "logName": "projects/local/logs/sev-test",
        "entries": [
            {"severity": "DEBUG", "jsonPayload": {}},
            {"severity": "CRITICAL", "jsonPayload": {}},
        ],
    })
    resp = _get(server,
                "/logging/entries?logName=projects/local/logs/sev-test"
                "&severityMin=WARNING")
    for entry in resp["entries"]:
        assert entry["severity"] >= 400


def test_logging_list_log_names(server):
    _post(server, "/logging/entries:write", {
        "logName": "projects/local/logs/names-test",
        "entries": [{"jsonPayload": {}}],
    })
    resp = _get(server, "/logging/logs")
    assert "projects/local/logs/names-test" in resp["logNames"]


def test_logging_tail(server):
    _post(server, "/logging/entries:write", {
        "logName": "projects/local/logs/tail-test",
        "entries": [{"jsonPayload": {"i": i}} for i in range(5)],
    })
    resp = _get(server, "/logging/entries:tail?n=3")
    assert len(resp["entries"]) >= 1


def test_logging_delete_log(server):
    _post(server, "/logging/entries:write", {
        "logName": "projects/local/logs/del-log",
        "entries": [{"jsonPayload": {}}] * 2,
    })
    req = urllib.request.Request(
        _url(server, "/logging/logs/projects/local/logs/del-log"),
        method="DELETE",
    )
    with urllib.request.urlopen(req) as r:
        resp = json.loads(r.read())
    assert resp["deleted"] == 2


# ============================================================
# Cloud Monitoring
# ============================================================

def test_monitoring_create_descriptor(server):
    resp = _post(server, "/monitoring/metricDescriptors", {
        "type": "custom.googleapis.com/e2e_test",
        "displayName": "E2E Test",
        "valueType": "DOUBLE",
        "metricKind": "GAUGE",
    })
    assert resp["type"] == "custom.googleapis.com/e2e_test"


def test_monitoring_list_descriptors(server):
    _post(server, "/monitoring/metricDescriptors", {
        "type": "custom.googleapis.com/list_test",
        "valueType": "INT64",
    })
    resp = _get(server, "/monitoring/metricDescriptors")
    types = [d["type"] for d in resp["metricDescriptors"]]
    assert "custom.googleapis.com/list_test" in types


def test_monitoring_write_and_list_time_series(server):
    now = time.time()
    _post(server, "/monitoring/timeSeries", {
        "timeSeries": [{
            "metric": {"type": "custom.googleapis.com/e2e_cpu",
                       "labels": {"host": "srv1"}},
            "resource": {"type": "global"},
            "points": [{
                "interval": {"startTime": now - 60, "endTime": now},
                "value": {"doubleValue": 75.5},
            }],
        }],
    })
    resp = _get(server,
                "/monitoring/timeSeries"
                "?metricType=custom.googleapis.com/e2e_cpu")
    assert len(resp["timeSeries"]) >= 1
    ts = resp["timeSeries"][0]
    assert ts["metric"]["type"] == "custom.googleapis.com/e2e_cpu"


def test_monitoring_delete_descriptor(server):
    _post(server, "/monitoring/metricDescriptors", {
        "type": "custom.googleapis.com/todel",
        "valueType": "DOUBLE",
    })
    req = urllib.request.Request(
        _url(server, "/monitoring/metricDescriptors/custom.googleapis.com/todel"),
        method="DELETE",
    )
    with urllib.request.urlopen(req) as r:
        resp = json.loads(r.read())
    assert resp["deleted"] == "custom.googleapis.com/todel"


# ============================================================
# Identity Platform
# ============================================================

def test_identityplatform_signup(server):
    resp = _post(server, "/identityplatform/accounts:signUp", {
        "email": "e2e.signup@example.com",
        "password": "pass1234",
        "displayName": "E2E User",
    })
    assert "uid" in resp
    assert "idToken" in resp


def test_identityplatform_signin(server):
    _post(server, "/identityplatform/accounts:signUp", {
        "email": "e2e.signin@example.com",
        "password": "securepass",
    })
    resp = _post(server, "/identityplatform/accounts:signIn", {
        "email": "e2e.signin@example.com",
        "password": "securepass",
    })
    assert "idToken" in resp


def test_identityplatform_signin_wrong_password(server):
    _post(server, "/identityplatform/accounts:signUp", {
        "email": "e2e.badpw@example.com",
        "password": "rightpass",
    })
    code, body = _expect_error(
        server, "POST", "/identityplatform/accounts:signIn",
        {"email": "e2e.badpw@example.com", "password": "wrongpass"},
    )
    assert code in (409, 401)


def test_identityplatform_verify_token(server):
    resp = _post(server, "/identityplatform/accounts:signUp", {
        "email": "e2e.verify@example.com",
        "password": "pass1234",
    })
    verify = _post(server, "/identityplatform/accounts:verify", {
        "idToken": resp["idToken"],
    })
    assert verify["payload"]["sub"] == resp["uid"]


def test_identityplatform_get_user(server):
    resp = _post(server, "/identityplatform/accounts:signUp", {
        "email": "e2e.get@example.com",
        "password": "pass1234",
    })
    uid = resp["uid"]
    user = _get(server, f"/identityplatform/accounts/{uid}")
    assert user["email"] == "e2e.get@example.com"


def test_identityplatform_list_users(server):
    _post(server, "/identityplatform/accounts:signUp", {
        "email": "e2e.list1@example.com", "password": "pass1234"})
    _post(server, "/identityplatform/accounts:signUp", {
        "email": "e2e.list2@example.com", "password": "pass1234"})
    resp = _get(server, "/identityplatform/accounts")
    emails = {u["email"] for u in resp["users"]}
    assert "e2e.list1@example.com" in emails


def test_identityplatform_update_user(server):
    resp = _post(server, "/identityplatform/accounts:signUp", {
        "email": "e2e.update@example.com",
        "password": "pass1234",
    })
    uid = resp["uid"]
    updated = _patch(server, f"/identityplatform/accounts/{uid}",
                     {"displayName": "Updated"})
    assert updated["displayName"] == "Updated"


def test_identityplatform_delete_user(server):
    resp = _post(server, "/identityplatform/accounts:signUp", {
        "email": "e2e.delete@example.com",
        "password": "pass1234",
    })
    uid = resp["uid"]
    _delete(server, f"/identityplatform/accounts/{uid}")
    code, _ = _expect_error(server, "GET",
                             f"/identityplatform/accounts/{uid}")
    assert code == 404


def test_identityplatform_custom_token(server):
    resp = _post(server, "/identityplatform/accounts:signUp", {
        "email": "e2e.custom@example.com",
        "password": "pass1234",
    })
    uid = resp["uid"]
    tok_resp = _post(server, f"/identityplatform/accounts/{uid}/customToken",
                     {"claims": {"role": "admin"}})
    assert "customToken" in tok_resp
    verify = _post(server, "/identityplatform/accounts:verify",
                   {"idToken": tok_resp["customToken"]})
    assert verify["payload"]["role"] == "admin"
