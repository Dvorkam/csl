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

"""HTML login/logout pages for the browser frontend."""

import hashlib
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Cookie, Depends, Form, Request, Response, status
from fastapi.responses import RedirectResponse
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_station_lite.server.auth.jwt import (
    create_access_token,
    create_refresh_token,
    decode_refresh_token,
)
from control_station_lite.server.auth.password import verify_password
from control_station_lite.server.config import get_settings
from control_station_lite.server.db.models import RefreshToken, User
from control_station_lite.server.db.session import get_session
from control_station_lite.server.web import templates
from control_station_lite.server.web.deps import _ACCESS_COOKIE, pop_flash, set_flash

router = APIRouter(tags=["web"])

_REFRESH_COOKIE = "refresh_token"
_COOKIE_MAX_AGE = 14 * 24 * 60 * 60
_ACCESS_MAX_AGE = 30 * 60


def _hash_jti(jti: str) -> str:
    return hashlib.sha256(jti.encode()).hexdigest()


async def _store_refresh_token(session: AsyncSession, user_id: int, jti: str) -> None:
    expires_at = datetime.now(UTC) + timedelta(days=14)
    session.add(
        RefreshToken(
            user_id=user_id,
            token_hash=_hash_jti(jti),
            issued_at=datetime.now(UTC),
            expires_at=expires_at,
            revoked=False,
        )
    )
    await session.flush()


def _set_auth_cookies(response: Response, access_token: str, refresh_token: str) -> None:
    secure = get_settings().cookie_secure
    response.set_cookie(
        key=_ACCESS_COOKIE,
        value=access_token,
        httponly=True,
        secure=secure,
        samesite="strict",
        max_age=_ACCESS_MAX_AGE,
    )
    response.set_cookie(
        key=_REFRESH_COOKIE,
        value=refresh_token,
        httponly=True,
        secure=secure,
        samesite="strict",
        max_age=_COOKIE_MAX_AGE,
    )


def _clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(_ACCESS_COOKIE, samesite="strict")
    response.delete_cookie(_REFRESH_COOKIE, samesite="strict")


@router.get("/login", include_in_schema=False)
async def login_page(request: Request) -> Response:
    """Render the login form.  Redirect to / if already authenticated."""
    if request.cookies.get(_ACCESS_COOKIE):
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    flash = pop_flash(request, Response())
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "flash": flash,
            "error": None,
        },
    )


@router.post("/login", include_in_schema=False)
async def login_submit(
    request: Request,
    username: str = Form(),
    password: str = Form(),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Process the login form, set auth cookies, redirect to dashboard."""
    result = await session.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()

    if user is None or user.disabled or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "flash": None,
                "error": "Invalid username or password.",
            },
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    access_token = create_access_token(user.id, user.role)
    refresh_token, jti = create_refresh_token(user.id)
    await _store_refresh_token(session, user.id, jti)
    await session.commit()

    response: Response = RedirectResponse(url="/", status_code=status.HTTP_302_FOUND)
    _set_auth_cookies(response, access_token, refresh_token)
    return response


@router.post("/logout", include_in_schema=False)
async def logout(
    refresh_token: str | None = Cookie(default=None, alias=_REFRESH_COOKIE),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Revoke refresh token, clear cookies, redirect to /login."""
    if refresh_token is not None:
        try:
            token_data = decode_refresh_token(refresh_token)
            result = await session.execute(
                select(RefreshToken).where(RefreshToken.token_hash == _hash_jti(token_data.jti))
            )
            row = result.scalar_one_or_none()
            if row is not None:
                row.revoked = True
            await session.commit()
        except JWTError:
            pass

    response: Response = RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)
    _clear_auth_cookies(response)
    set_flash(response, "You have been logged out.", "info")
    return response
