"""Tests for Identity Platform auth emulator (opengcp.identityplatform)."""

import pytest

from opengcp.identityplatform import (
    IdentityPlatform, AuthError, UserNotFound,
    EmailExistsError, InvalidCredentials,
    TokenExpiredError, TokenInvalidError,
)


@pytest.fixture()
def auth():
    return IdentityPlatform()


# ----- sign-up -----

def test_sign_up_returns_uid_and_token(auth):
    result = auth.sign_up("alice@example.com", "password123")
    assert "uid" in result
    assert "idToken" in result
    assert result["email"] == "alice@example.com"


def test_sign_up_duplicate_email_fails(auth):
    auth.sign_up("dup@example.com", "pass1234")
    with pytest.raises(EmailExistsError):
        auth.sign_up("dup@example.com", "pass1234")


def test_sign_up_invalid_email(auth):
    with pytest.raises(AuthError):
        auth.sign_up("notanemail", "pass1234")


def test_sign_up_short_password(auth):
    with pytest.raises(AuthError):
        auth.sign_up("ok@example.com", "123")


def test_sign_up_with_display_name(auth):
    result = auth.sign_up("named@example.com", "pass1234",
                           display_name="Alice Smith")
    assert result["displayName"] == "Alice Smith"


# ----- sign-in -----

def test_sign_in_success(auth):
    auth.sign_up("bob@example.com", "securepass")
    result = auth.sign_in("bob@example.com", "securepass")
    assert "idToken" in result
    assert result["email"] == "bob@example.com"


def test_sign_in_wrong_password(auth):
    auth.sign_up("carol@example.com", "rightpass")
    with pytest.raises(InvalidCredentials):
        auth.sign_in("carol@example.com", "wrongpass")


def test_sign_in_unknown_user(auth):
    with pytest.raises(InvalidCredentials):
        auth.sign_in("nobody@example.com", "x")


def test_sign_in_disabled_user(auth):
    result = auth.sign_up("disabled@example.com", "pass1234")
    uid = result["uid"]
    auth.update_user(uid, disabled=True)
    with pytest.raises(AuthError):
        auth.sign_in("disabled@example.com", "pass1234")


# ----- token verification -----

def test_verify_valid_token(auth):
    result = auth.sign_up("verify@example.com", "pass1234")
    payload = auth.verify_id_token(result["idToken"])
    assert payload["sub"] == result["uid"]
    assert payload["email"] == "verify@example.com"


def test_verify_tampered_token(auth):
    result = auth.sign_up("tamper@example.com", "pass1234")
    token = result["idToken"]
    # tamper with the signature
    parts = token.split(".")
    parts[-1] = "invalidsignature"
    with pytest.raises(TokenInvalidError):
        auth.verify_id_token(".".join(parts))


def test_verify_malformed_token(auth):
    with pytest.raises(TokenInvalidError):
        auth.verify_id_token("not.a.valid.jwt.token")


def test_verify_expired_token(auth):
    # Create a token with TTL=0 (already expired)
    from opengcp.identityplatform import _make_token
    result = auth.sign_up("exp@example.com", "pass1234")
    expired_token = _make_token(auth._secret, result["uid"],
                                result["email"], ttl=-1)
    with pytest.raises(TokenExpiredError):
        auth.verify_id_token(expired_token)


# ----- user management -----

def test_get_user(auth):
    result = auth.sign_up("getme@example.com", "pass1234")
    user = auth.get_user(result["uid"])
    assert user["email"] == "getme@example.com"


def test_get_user_by_email(auth):
    auth.sign_up("byemail@example.com", "pass1234")
    user = auth.get_user_by_email("byemail@example.com")
    assert user["email"] == "byemail@example.com"


def test_get_nonexistent_user(auth):
    with pytest.raises(UserNotFound):
        auth.get_user("no-such-uid")


def test_update_display_name(auth):
    result = auth.sign_up("update@example.com", "pass1234")
    uid = result["uid"]
    auth.update_user(uid, display_name="Updated Name")
    user = auth.get_user(uid)
    assert user["displayName"] == "Updated Name"


def test_update_password(auth):
    result = auth.sign_up("pwchange@example.com", "oldpass1")
    uid = result["uid"]
    auth.update_user(uid, password="newpass2")
    # old password should fail
    with pytest.raises(InvalidCredentials):
        auth.sign_in("pwchange@example.com", "oldpass1")
    # new password should work
    r2 = auth.sign_in("pwchange@example.com", "newpass2")
    assert "idToken" in r2


def test_delete_user(auth):
    result = auth.sign_up("delete@example.com", "pass1234")
    uid = result["uid"]
    auth.delete_user(uid)
    with pytest.raises(UserNotFound):
        auth.get_user(uid)


def test_delete_nonexistent_user(auth):
    with pytest.raises(UserNotFound):
        auth.delete_user("ghost-uid")


def test_list_users(auth):
    auth.sign_up("list1@example.com", "pass1234")
    auth.sign_up("list2@example.com", "pass1234")
    users = auth.list_users()
    emails = {u["email"] for u in users}
    assert "list1@example.com" in emails
    assert "list2@example.com" in emails


# ----- custom tokens -----

def test_create_custom_token(auth):
    result = auth.sign_up("custom@example.com", "pass1234")
    uid = result["uid"]
    token = auth.create_custom_token(uid)
    payload = auth.verify_id_token(token)
    assert payload["sub"] == uid


def test_custom_token_with_claims(auth):
    result = auth.sign_up("claims@example.com", "pass1234")
    uid = result["uid"]
    token = auth.create_custom_token(uid, claims={"role": "admin"})
    payload = auth.verify_id_token(token)
    assert payload.get("role") == "admin"


def test_custom_token_nonexistent_user(auth):
    with pytest.raises(UserNotFound):
        auth.create_custom_token("no-uid")
