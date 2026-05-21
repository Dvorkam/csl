"""Unit tests for server/core/crypto.py (AES-256-GCM encrypt/decrypt)."""

import os

import pytest
from cryptography.exceptions import InvalidTag

from control_station_lite.server.core.crypto import _NONCE_SIZE, decrypt, encrypt

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _key(size: int = 32) -> bytes:
    return os.urandom(size)


# ---------------------------------------------------------------------------
# encrypt
# ---------------------------------------------------------------------------


def test_encrypt_returns_bytes() -> None:
    assert isinstance(encrypt(b"hello", _key()), bytes)


def test_encrypt_output_length() -> None:
    plaintext = b"hello world"
    # nonce (12) + plaintext + GCM tag (16)
    assert len(encrypt(plaintext, _key())) == _NONCE_SIZE + len(plaintext) + 16


def test_encrypt_empty_plaintext() -> None:
    # Empty plaintext: only nonce + tag
    result = encrypt(b"", _key())
    assert len(result) == _NONCE_SIZE + 16


def test_encrypt_different_nonces_each_call() -> None:
    key = _key()
    a = encrypt(b"same", key)
    b = encrypt(b"same", key)
    # Nonces must differ — same nonce reuse would be a catastrophic AES-GCM failure
    assert a[:_NONCE_SIZE] != b[:_NONCE_SIZE]


def test_encrypt_different_ciphertexts_each_call() -> None:
    key = _key()
    assert encrypt(b"same", key) != encrypt(b"same", key)


def test_encrypt_wrong_key_size_raises() -> None:
    with pytest.raises(ValueError, match="128, 192, or 256 bits"):
        encrypt(b"data", os.urandom(31))  # 31 bytes — not 128/192/256 bits


# ---------------------------------------------------------------------------
# decrypt — happy path
# ---------------------------------------------------------------------------


def test_decrypt_round_trip() -> None:
    key = _key()
    plaintext = b"secret payload"
    assert decrypt(encrypt(plaintext, key), key) == plaintext


def test_decrypt_round_trip_empty_plaintext() -> None:
    key = _key()
    assert decrypt(encrypt(b"", key), key) == b""


def test_decrypt_round_trip_long_payload() -> None:
    key = _key()
    plaintext = os.urandom(10_000)
    assert decrypt(encrypt(plaintext, key), key) == plaintext


# ---------------------------------------------------------------------------
# decrypt — error cases
# ---------------------------------------------------------------------------


def test_decrypt_wrong_key_raises_invalid_tag() -> None:
    blob = encrypt(b"secret", _key())
    with pytest.raises(InvalidTag):
        decrypt(blob, _key())  # different key


def test_decrypt_tampered_ciphertext_raises_invalid_tag() -> None:
    key = _key()
    blob = bytearray(encrypt(b"secret", key))
    blob[-1] ^= 0xFF  # flip a bit in the tag
    with pytest.raises(InvalidTag):
        decrypt(bytes(blob), key)


def test_decrypt_tampered_nonce_raises_invalid_tag() -> None:
    key = _key()
    blob = bytearray(encrypt(b"secret", key))
    blob[0] ^= 0xFF  # flip a bit in the nonce
    with pytest.raises(InvalidTag):
        decrypt(bytes(blob), key)


def test_decrypt_truncated_blob_raises_value_error() -> None:
    with pytest.raises(ValueError, match="too short"):
        decrypt(b"\x00" * (_NONCE_SIZE - 1), _key())


def test_decrypt_empty_blob_raises_value_error() -> None:
    with pytest.raises(ValueError, match="too short"):
        decrypt(b"", _key())


def test_decrypt_wrong_key_size_raises() -> None:
    blob = encrypt(b"data", _key())
    with pytest.raises(ValueError, match="128, 192, or 256 bits"):
        decrypt(blob, os.urandom(31))
