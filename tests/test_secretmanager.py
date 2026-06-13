"""Tests for Secret Manager (opengcp.secretmanager)."""

import pytest

from opengcp.secretmanager import (
    SecretManager, SecretManagerError, SecretNotFound,
    VersionNotFound, VersionStateError,
)


@pytest.fixture()
def sm():
    return SecretManager()


# ----- secrets -----

def test_create_and_get_secret(sm):
    secret = sm.create_secret("my-secret", labels={"env": "test"})
    assert secret["secretId"] == "my-secret"
    assert secret["labels"]["env"] == "test"
    fetched = sm.get_secret("my-secret")
    assert fetched["secretId"] == "my-secret"


def test_create_duplicate_secret_fails(sm):
    sm.create_secret("dup")
    with pytest.raises(SecretManagerError):
        sm.create_secret("dup")


def test_list_secrets(sm):
    sm.create_secret("alpha")
    sm.create_secret("beta")
    secrets = sm.list_secrets()
    ids = [s["secretId"] for s in secrets]
    assert "alpha" in ids
    assert "beta" in ids


def test_get_nonexistent_secret_raises(sm):
    with pytest.raises(SecretNotFound):
        sm.get_secret("no-such-secret")


def test_delete_secret(sm):
    sm.create_secret("todel")
    sm.delete_secret("todel")
    with pytest.raises(SecretNotFound):
        sm.get_secret("todel")


def test_delete_nonexistent_raises(sm):
    with pytest.raises(SecretNotFound):
        sm.delete_secret("ghost")


# ----- versions -----

def test_add_and_access_version(sm):
    sm.create_secret("sec1")
    ver = sm.add_version("sec1", b"my secret value")
    assert ver["version"] == 1
    assert ver["state"] == "ENABLED"
    payload = sm.access_version("sec1", "1")
    assert payload == b"my secret value"


def test_access_latest(sm):
    sm.create_secret("sec2")
    sm.add_version("sec2", b"v1")
    sm.add_version("sec2", b"v2")
    payload = sm.access_version("sec2", "latest")
    assert payload == b"v2"


def test_multiple_versions(sm):
    sm.create_secret("sec3")
    for i in range(3):
        sm.add_version("sec3", f"value{i}".encode())
    versions = sm.list_versions("sec3")
    assert len(versions) == 3
    assert versions[0]["version"] == 1


def test_list_versions_on_unknown_secret(sm):
    with pytest.raises(SecretNotFound):
        sm.list_versions("nope")


def test_get_version_metadata(sm):
    sm.create_secret("sec4")
    sm.add_version("sec4", b"payload")
    meta = sm.get_version("sec4", "1")
    assert meta["state"] == "ENABLED"
    assert meta["version"] == 1


def test_disable_and_enable_version(sm):
    sm.create_secret("sec5")
    sm.add_version("sec5", b"data")
    sm.disable_version("sec5", "1")
    with pytest.raises(VersionStateError):
        sm.access_version("sec5", "1")
    sm.enable_version("sec5", "1")
    assert sm.access_version("sec5", "1") == b"data"


def test_destroy_version(sm):
    sm.create_secret("sec6")
    sm.add_version("sec6", b"secret")
    sm.destroy_version("sec6", "1")
    meta = sm.get_version("sec6", "1")
    assert meta["state"] == "DESTROYED"
    with pytest.raises(VersionStateError):
        sm.access_version("sec6", "1")


def test_cannot_enable_destroyed_version(sm):
    sm.create_secret("sec7")
    sm.add_version("sec7", b"x")
    sm.destroy_version("sec7", "1")
    with pytest.raises(VersionStateError):
        sm.enable_version("sec7", "1")


def test_add_version_to_nonexistent_secret(sm):
    with pytest.raises(SecretNotFound):
        sm.add_version("ghost", b"x")


def test_latest_skips_destroyed(sm):
    sm.create_secret("sec8")
    sm.add_version("sec8", b"v1")
    sm.add_version("sec8", b"v2")
    sm.destroy_version("sec8", "2")
    payload = sm.access_version("sec8", "latest")
    assert payload == b"v1"
