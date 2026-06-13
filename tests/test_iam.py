"""Tests for the IAM service (opengcp.iam)."""

import pytest

from opengcp.iam import IAMService, IAMError, RoleNotFound


# ----- fixtures -----

@pytest.fixture()
def iam():
    return IAMService()


# ----- predefined roles -----

def test_predefined_roles_seeded(iam):
    roles = iam.list_roles()
    names = {r["name"] for r in roles}
    assert "roles/viewer" in names
    assert "roles/editor" in names
    assert "roles/owner" in names


def test_get_predefined_role(iam):
    role = iam.get_role("roles/viewer")
    assert role["name"] == "roles/viewer"
    assert "storage.objects.get" in role["permissions"]


def test_predefined_role_not_found(iam):
    with pytest.raises(RoleNotFound):
        iam.get_role("roles/nonexistent")


# ----- custom roles -----

def test_create_custom_role(iam):
    role = iam.create_role(
        "roles/myapp.reader",
        title="MyApp Reader",
        permissions=["myapp.data.read"],
    )
    assert role["name"] == "roles/myapp.reader"
    assert "myapp.data.read" in role["permissions"]


def test_create_duplicate_role_fails(iam):
    iam.create_role("roles/test.role", permissions=[])
    with pytest.raises(IAMError):
        iam.create_role("roles/test.role", permissions=[])


def test_update_role(iam):
    iam.create_role("roles/patch.me", title="Old", permissions=["x.y.z"])
    updated = iam.update_role("roles/patch.me", title="New",
                              permissions=["x.y.z", "a.b.c"])
    assert updated["title"] == "New"
    assert "a.b.c" in updated["permissions"]


def test_delete_custom_role(iam):
    iam.create_role("roles/todelete")
    iam.delete_role("roles/todelete")
    with pytest.raises(RoleNotFound):
        iam.get_role("roles/todelete")


def test_cannot_delete_predefined_role(iam):
    with pytest.raises(IAMError):
        iam.delete_role("roles/viewer")


# ----- resources -----

def test_register_and_list_resource(iam):
    iam.register_resource("projects/myproject", "project")
    resources = iam.list_resources()
    names = [r["name"] for r in resources]
    assert "projects/myproject" in names


def test_register_resource_idempotent(iam):
    iam.register_resource("projects/dup")
    iam.register_resource("projects/dup")  # should not error
    resources = iam.list_resources()
    assert sum(1 for r in resources if r["name"] == "projects/dup") == 1


# ----- policy -----

def test_get_empty_policy(iam):
    policy = iam.get_iam_policy("projects/empty")
    assert policy["bindings"] == []


def test_set_and_get_policy(iam):
    bindings = [
        {"role": "roles/viewer", "members": ["user:alice@example.com"]},
    ]
    policy = iam.set_iam_policy("projects/test", bindings)
    assert len(policy["bindings"]) == 1
    assert policy["bindings"][0]["role"] == "roles/viewer"


def test_set_policy_replaces_existing(iam):
    iam.set_iam_policy("projects/replace", [
        {"role": "roles/viewer", "members": ["user:alice@example.com"]}
    ])
    iam.set_iam_policy("projects/replace", [
        {"role": "roles/editor", "members": ["user:bob@example.com"]}
    ])
    policy = iam.get_iam_policy("projects/replace")
    roles = {b["role"] for b in policy["bindings"]}
    assert "roles/viewer" not in roles
    assert "roles/editor" in roles


# ----- testIamPermissions -----

def test_test_iam_permissions_granted(iam):
    iam.set_iam_policy("projects/p1", [
        {"role": "roles/viewer", "members": ["user:alice@example.com"]}
    ])
    result = iam.test_iam_permissions(
        "projects/p1",
        "user:alice@example.com",
        ["storage.objects.get", "storage.objects.delete"],
    )
    assert "storage.objects.get" in result
    assert "storage.objects.delete" not in result


def test_test_iam_permissions_no_binding(iam):
    result = iam.test_iam_permissions(
        "projects/empty",
        "user:nobody@example.com",
        ["storage.objects.get"],
    )
    assert result == []


def test_allusers_wildcard(iam):
    iam.set_iam_policy("projects/public", [
        {"role": "roles/viewer", "members": ["allUsers"]}
    ])
    result = iam.test_iam_permissions(
        "projects/public",
        "user:anyone@example.com",
        ["storage.objects.get"],
    )
    assert "storage.objects.get" in result


def test_owner_has_all_permissions(iam):
    iam.set_iam_policy("projects/owned", [
        {"role": "roles/owner", "members": ["user:admin@example.com"]}
    ])
    result = iam.test_iam_permissions(
        "projects/owned",
        "user:admin@example.com",
        ["storage.objects.create", "iam.roles.delete",
         "secretmanager.secrets.delete"],
    )
    assert len(result) == 3
