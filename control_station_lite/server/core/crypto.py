# SPDX-License-Identifier: AGPL-3.0-or-later
#
# control-station-lite
# Copyright (C) 2026 Michal Dvořák
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version, with an additional permission for
# distribution through app stores (see LICENSE).

import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_SIZE = 12  # 96-bit nonce — standard for AES-GCM


def encrypt(plaintext: bytes, master_key: bytes) -> bytes:
    """Encrypt *plaintext* with AES-256-GCM.

    Returns ``nonce || ciphertext+tag`` (nonce is 12 random bytes prepended).
    A fresh nonce is generated for every call, so identical plaintexts produce
    different output — callers must not reuse the returned blob as a key.
    """
    nonce = os.urandom(_NONCE_SIZE)
    return nonce + AESGCM(master_key).encrypt(nonce, plaintext, None)


def decrypt(data: bytes, master_key: bytes) -> bytes:
    """Decrypt AES-256-GCM blob produced by :func:`encrypt`.

    *data* must be at least ``_NONCE_SIZE`` bytes (nonce prefix).
    Raises :class:`cryptography.exceptions.InvalidTag` if the key is wrong
    or the ciphertext has been tampered with.
    Raises :class:`ValueError` if *data* is too short to contain a nonce.
    """
    if len(data) < _NONCE_SIZE:
        raise ValueError(
            f"encrypted blob too short: expected at least {_NONCE_SIZE} bytes, got {len(data)}"
        )
    nonce, ciphertext = data[:_NONCE_SIZE], data[_NONCE_SIZE:]
    return AESGCM(master_key).decrypt(nonce, ciphertext, None)
