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

import hashlib
from datetime import UTC, datetime

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from jose import JWTError
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_station_lite.server.auth.jwt import (
    create_access_token,
    create_refresh_token,
    decode_refresh_token,
)
from control_station_lite.server.auth.password import verify_password
from control_station_lite.server.db.models import RefreshToken, User
from control_station_lite.server.db.session import get_session

router = APIRouter(prefix="/api/auth", tags=["auth"])

_REFRESH_COOKIE = "refresh_token"
_COOKIE_MAX_AGE = 14 * 24 * 60 * 60  # 14 days in seconds


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash_jti(jti: str) -> str:
    return hashlib.sha256(jti.encode()).hexdigest()


def _set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=_REFRESH_COOKIE,
        value=token,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=_COOKIE_MAX_AGE,
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(key=_REFRESH_COOKIE, httponly=True, secure=True, samesite="strict")


async def _store_refresh_token(session: AsyncSession, user_id: int, jti: str, token: str) -> None:
    from datetime import timedelta

    expires_at = datetime.now(UTC) + timedelta(days=14)
    row = RefreshToken(
        user_id=user_id,
        token_hash=_hash_jti(jti),
        issued_at=datetime.now(UTC),
        expires_at=expires_at,
        revoked=False,
    )
    session.add(row)
    await session.flush()


async def _revoke_jti(session: AsyncSession, jti: str) -> None:
    result = await session.execute(
        select(RefreshToken).where(RefreshToken.token_hash == _hash_jti(jti))
    )
    row = result.scalar_one_or_none()
    if row is not None:
        row.revoked = True
        await session.flush()


async def _validate_refresh_jti(session: AsyncSession, jti: str) -> None:
    """Raise 401 if the JTI is unknown, revoked, or expired."""
    result = await session.execute(
        select(RefreshToken).where(RefreshToken.token_hash == _hash_jti(jti))
    )
    row = result.scalar_one_or_none()
    if row is None or row.revoked:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token revoked")
    if row.expires_at.replace(tzinfo=UTC) < datetime.now(UTC):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    result = await session.execute(select(User).where(User.username == body.username))
    user = result.scalar_one_or_none()
    if user is None or user.disabled or not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
        )
    access_token = create_access_token(user.id, user.role)
    refresh_token, jti = create_refresh_token(user.id)
    await _store_refresh_token(session, user.id, jti, refresh_token)
    await session.commit()
    _set_refresh_cookie(response, refresh_token)
    return TokenResponse(access_token=access_token)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    response: Response,
    refresh_token: str | None = Cookie(default=None, alias=_REFRESH_COOKIE),
    session: AsyncSession = Depends(get_session),
) -> TokenResponse:
    if refresh_token is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No refresh token")
    try:
        token_data = decode_refresh_token(refresh_token)
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        ) from None
    await _validate_refresh_jti(session, token_data.jti)

    result = await session.execute(select(User).where(User.id == token_data.user_id))
    user = result.scalar_one_or_none()
    if user is None or user.disabled:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    # Rotate: revoke old, issue new
    await _revoke_jti(session, token_data.jti)
    new_access = create_access_token(user.id, user.role)
    new_refresh, new_jti = create_refresh_token(user.id)
    await _store_refresh_token(session, user.id, new_jti, new_refresh)
    await session.commit()
    _set_refresh_cookie(response, new_refresh)
    return TokenResponse(access_token=new_access)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    refresh_token: str | None = Cookie(default=None, alias=_REFRESH_COOKIE),
    session: AsyncSession = Depends(get_session),
) -> None:
    if refresh_token is not None:
        try:
            token_data = decode_refresh_token(refresh_token)
            await _revoke_jti(session, token_data.jti)
            await session.commit()
        except JWTError:
            pass  # malformed token — clear cookie anyway
    _clear_refresh_cookie(response)
