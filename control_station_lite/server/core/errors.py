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

"""Stable error-code catalogue and structured error responses (ARCHITECTURE §10).

Responses carry a machine-stable ``code`` field alongside the human-readable
``detail`` that FastAPI already returns, so clients can branch on ``code``
without string-matching messages. The ``detail`` shape is preserved for
backward compatibility.
"""

from enum import StrEnum
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class ErrorCode(StrEnum):
    """Stable error codes. Values are part of the API contract — append, never rename."""

    # auth
    AUTH_INVALID_CREDENTIALS = "auth.invalid_credentials"
    AUTH_TOKEN_INVALID = "auth.token_invalid"
    AUTH_TOKEN_REVOKED = "auth.token_revoked"
    AUTH_TOKEN_EXPIRED = "auth.token_expired"
    AUTH_FORBIDDEN = "auth.forbidden"
    # approval
    APPROVAL_PENDING = "approval.pending"
    APPROVAL_REJECTED = "approval.rejected"
    # agent
    AGENT_UNREACHABLE = "agent.unreachable"
    # validation
    VALIDATION_ERROR = "validation.error"
    # compatibility
    VERSION_INCOMPATIBLE = "version.incompatible"


class CslHTTPException(HTTPException):
    """An :class:`HTTPException` carrying a stable :class:`ErrorCode`.

    *detail* keeps its usual role (string or dict) so existing clients that read
    ``response.json()["detail"]`` keep working; the handler adds ``code`` and
    merges any *extra* keys at the top level of the response body.
    """

    def __init__(
        self,
        status_code: int,
        code: ErrorCode,
        detail: Any,
        *,
        extra: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.code = code
        self.extra = extra or {}


async def _csl_exception_handler(_request: Request, exc: CslHTTPException) -> JSONResponse:
    body: dict[str, Any] = {"detail": exc.detail, "code": str(exc.code), **exc.extra}
    return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)


async def _validation_exception_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={
            "detail": jsonable_encoder(exc.errors()),
            "code": str(ErrorCode.VALIDATION_ERROR),
        },
    )


def install_error_handlers(app: FastAPI) -> None:
    """Register the structured-error handlers on *app*."""
    app.add_exception_handler(CslHTTPException, _csl_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(
        RequestValidationError,
        _validation_exception_handler,  # type: ignore[arg-type]
    )
