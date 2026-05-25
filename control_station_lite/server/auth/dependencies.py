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

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_station_lite.server.auth.jwt import decode_access_token
from control_station_lite.server.db.models import User
from control_station_lite.server.db.session import get_session

_bearer = HTTPBearer()


async def current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> User:
    """FastAPI dependency: decode Bearer token and return the matching User row.

    Raises 401 if the token is missing, malformed, expired, or the user no
    longer exists or is disabled.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        token_data = decode_access_token(credentials.credentials)
    except JWTError:
        raise credentials_exception from None

    result = await session.execute(select(User).where(User.id == token_data.user_id))
    user = result.scalar_one_or_none()
    if user is None or user.disabled:
        raise credentials_exception
    return user


async def require_admin(user: User = Depends(current_user)) -> User:
    """FastAPI dependency: like *current_user* but raises 403 for non-admins."""
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return user
