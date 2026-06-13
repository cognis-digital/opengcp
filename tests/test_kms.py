"""Tests for Cloud KMS emulator (opengcp.kms)."""

import base64

import pytest

from opengcp.kms import KMSService, KMSError, KeyRingNotFound, CryptoKeyNotFound


@pytest.fixture()
def kms():
    return KMSService()


@pytest.fixture()
def kms_with_key(kms):
    kms.create_key_ring("ring1")
    kms.create_crypto_key("ring1", "key1")
    return kms


# ----- key rings -----

def test_create_and_get_key_ring(kms):
    ring = kms.create_key_ring("my-ring")
    assert ring["name"] == "my-ring"
    fetched = kms.get_key_ring("my-ring")
    assert fetched["name"] == "my-ring"


def test_create_duplicate_key_ring_fails(kms):
    kms.create_key_ring("dup")
    with pytest.raises(KMSError):
        kms.create_key_ring("dup")


def test_list_key_rings(kms):
    kms.create_key_ring("ring-a")
    kms.create_key_ring("ring-b")
    rings = kms.list_key_rings()
    names = [r["name"] for r in rings]
    assert "ring-a" in names and "ring-b" in names


def test_get_nonexistent_key_ring(kms):
    with pytest.raises(KeyRingNotFound):
        kms.get_key_ring("nope")


# ----- crypto keys -----

def test_create_and_get_crypto_key(kms):
    kms.create_key_ring("ring2")
    key = kms.create_crypto_key("ring2", "mykey")
    assert key["keyId"] == "mykey"
    assert key["primaryVersion"] == 1
    fetched = kms.get_crypto_key("ring2", "mykey")
    assert fetched["keyId"] == "mykey"


def test_create_key_in_nonexistent_ring(kms):
    with pytest.raises(KeyRingNotFound):
        kms.create_crypto_key("no-ring", "k")


def test_create_duplicate_key_fails(kms):
    kms.create_key_ring("ring3")
    kms.create_crypto_key("ring3", "k")
    with pytest.raises(KMSError):
        kms.create_crypto_key("ring3", "k")


def test_list_crypto_keys(kms):
    kms.create_key_ring("ring4")
    kms.create_crypto_key("ring4", "k1")
    kms.create_crypto_key("ring4", "k2")
    keys = kms.list_crypto_keys("ring4")
    ids = [k["keyId"] for k in keys]
    assert "k1" in ids and "k2" in ids


def test_list_crypto_key_versions(kms_with_key):
    versions = kms_with_key.list_crypto_key_versions("ring1", "key1")
    assert len(versions) == 1
    assert versions[0]["version"] == 1
    assert versions[0]["state"] == "ENABLED"


# ----- encrypt / decrypt -----

def test_encrypt_and_decrypt_roundtrip(kms_with_key):
    plaintext = b"Hello, opengcp KMS!"
    enc = kms_with_key.encrypt("ring1", "key1", plaintext)
    assert "ciphertext" in enc
    decrypted = kms_with_key.decrypt("ring1", "key1", enc["ciphertext"])
    assert decrypted == plaintext


def test_encrypt_with_aad(kms_with_key):
    plaintext = b"secret data"
    aad = b"project/123"
    enc = kms_with_key.encrypt("ring1", "key1", plaintext, aad)
    decrypted = kms_with_key.decrypt("ring1", "key1", enc["ciphertext"], aad)
    assert decrypted == plaintext


def test_decrypt_wrong_aad_fails(kms_with_key):
    enc = kms_with_key.encrypt("ring1", "key1", b"secret", b"correct-aad")
    with pytest.raises(KMSError):
        kms_with_key.decrypt("ring1", "key1", enc["ciphertext"], b"wrong-aad")


def test_decrypt_tampered_ciphertext_fails(kms_with_key):
    enc = kms_with_key.encrypt("ring1", "key1", b"data")
    ct = base64.b64decode(enc["ciphertext"])
    # flip a byte in the ciphertext part
    tampered = bytearray(ct)
    tampered[-1] ^= 0xFF
    with pytest.raises(KMSError):
        kms_with_key.decrypt("ring1", "key1",
                              base64.b64encode(bytes(tampered)).decode())


def test_encrypt_nonexistent_key(kms):
    kms.create_key_ring("ring5")
    with pytest.raises(CryptoKeyNotFound):
        kms.encrypt("ring5", "no-key", b"x")


def test_empty_plaintext(kms_with_key):
    enc = kms_with_key.encrypt("ring1", "key1", b"")
    decrypted = kms_with_key.decrypt("ring1", "key1", enc["ciphertext"])
    assert decrypted == b""


# ----- generate data key -----

def test_generate_data_key(kms_with_key):
    result = kms_with_key.generate_data_key("ring1", "key1")
    assert "plaintext" in result
    assert "ciphertextBlob" in result
    dek = base64.b64decode(result["plaintext"])
    assert len(dek) == 32


def test_data_key_wrapped_decryptable(kms_with_key):
    result = kms_with_key.generate_data_key("ring1", "key1")
    plaintext_dek = base64.b64decode(result["plaintext"])
    # unwrap via decrypt
    decrypted_dek = kms_with_key.decrypt("ring1", "key1", result["ciphertextBlob"])
    assert decrypted_dek == plaintext_dek
