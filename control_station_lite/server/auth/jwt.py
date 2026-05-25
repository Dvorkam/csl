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

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from jose import JWTError, jwt
from pydantic import BaseModel

from control_station_lite.server.config import get_settings

_ALGORITHM = "HS256"
_ACCESS_TOKEN_EXPIRE_MINUTES = 30
_REFRESH_TOKEN_EXPIRE_DAYS = 14


class AccessTokenData(BaseModel):
    user_id: int
    role: str


class RefreshTokenData(BaseModel):
    user_id: int
    jti: str


def _secret() -> str:
    return get_settings().read_jwt_key().decode(errors="replace")


def create_access_token(user_id: int, role: str) -> str:
    """Issue a signed HS256 access token valid for 30 minutes."""
    expire = datetime.now(UTC) + timedelta(minutes=_ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": str(user_id),
        "role": role,
        "exp": expire,
        "type": "access",
    }
    return str(jwt.encode(payload, _secret(), algorithm=_ALGORITHM))


def create_refresh_token(user_id: int) -> tuple[str, str]:
    """Issue a signed HS256 refresh token valid for 14 days.

    Returns ``(token, jti)`` — the JTI is the unique identifier whose hash
    the caller must store in ``refresh_tokens``.
    """
    jti = str(uuid4())
    expire = datetime.now(UTC) + timedelta(days=_REFRESH_TOKEN_EXPIRE_DAYS)
    payload = {
        "sub": str(user_id),
        "jti": jti,
        "exp": expire,
        "type": "refresh",
    }
    return str(jwt.encode(payload, _secret(), algorithm=_ALGORITHM)), jti


def _decode(token: str, expected_type: str) -> dict[str, Any]:
    """Decode, verify signature/expiry, and assert *expected_type*.

    Raises :class:`jose.JWTError` on any failure so callers have a single
    exception type to handle.
    """
    payload: dict[str, Any] = jwt.decode(token, _secret(), algorithms=[_ALGORITHM])
    if payload.get("type") != expected_type:
        raise JWTError(f"expected token type {expected_type!r}")
    return payload


def decode_access_token(token: str) -> AccessTokenData:
    """Decode and validate an access token; raises :class:`~jose.JWTError` on failure."""
    payload = _decode(token, "access")
    return AccessTokenData(user_id=int(payload["sub"]), role=payload["role"])


def decode_refresh_token(token: str) -> RefreshTokenData:
    """Decode and validate a refresh token; raises :class:`~jose.JWTError` on failure."""
    payload = _decode(token, "refresh")
    return RefreshTokenData(user_id=int(payload["sub"]), jti=payload["jti"])
