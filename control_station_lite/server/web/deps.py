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

"""FastAPI dependencies for browser-facing web routes.

Web routes use an HttpOnly ``csl_access`` cookie instead of a Bearer header,
so they need a separate dependency from the JSON API layer.
"""

from fastapi import Cookie, Depends, HTTPException, Request, Response, status
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_station_lite.server.auth.jwt import decode_access_token
from control_station_lite.server.db.models import User
from control_station_lite.server.db.session import get_session

_ACCESS_COOKIE = "csl_access"


async def web_current_user(
    csl_access: str | None = Cookie(default=None),
    session: AsyncSession = Depends(get_session),
) -> User:
    """Return the authenticated User from the ``csl_access`` cookie.

    Redirects to /login (302) if the cookie is absent, invalid, or expired.
    """
    if csl_access is None:
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            headers={"Location": "/login"},
        )
    try:
        token_data = decode_access_token(csl_access)
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            headers={"Location": "/login"},
        ) from None
    result = await session.execute(select(User).where(User.id == token_data.user_id))
    user = result.scalar_one_or_none()
    if user is None or user.disabled:
        raise HTTPException(
            status_code=status.HTTP_302_FOUND,
            headers={"Location": "/login"},
        )
    return user


async def web_require_admin(user: User = Depends(web_current_user)) -> User:
    """Like ``web_current_user`` but additionally requires admin role."""
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return user


def pop_flash(request: Request, response: Response) -> tuple[str, str] | None:
    """Read the ``_flash`` cookie, schedule its deletion, and return (category, message)."""
    raw = request.cookies.get("_flash")
    response.delete_cookie("_flash", samesite="strict")
    if raw and "|" in raw:
        cat, msg = raw.split("|", 1)
        return cat, msg
    return None


def set_flash(response: Response, message: str, category: str = "info") -> None:
    """Write a one-shot flash message into the ``_flash`` cookie."""
    response.set_cookie("_flash", f"{category}|{message}", httponly=True, samesite="strict")
