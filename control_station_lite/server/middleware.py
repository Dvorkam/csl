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

"""Correlation-id middleware (ARCHITECTURE §10).

Assigns a request id to every request, exposes it on the response, and makes it
available to log records and outbound agent calls via :data:`request_id_var`.

Implemented as pure ASGI (not ``BaseHTTPMiddleware``) so the contextvar is set
in the same task that runs the endpoint — ``BaseHTTPMiddleware`` runs the
downstream app in a child task, where context set before ``call_next`` is only
copied, not shared.
"""

import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from control_station_lite.server.logging_config import REQUEST_ID_HEADER, request_id_var

_HEADER_BYTES = REQUEST_ID_HEADER.lower().encode()


class RequestIdMiddleware:
    """Bind a correlation id to the request context for its whole lifetime.

    Honours an inbound ``X-Request-ID`` header (so a trace started elsewhere is
    preserved) and otherwise generates one. The id is reset on the way out so it
    never leaks into an unrelated context.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope["headers"]).get(_HEADER_BYTES)
        request_id = incoming.decode() if incoming else uuid.uuid4().hex
        token = request_id_var.set(request_id)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                headers.append((_HEADER_BYTES, request_id.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            request_id_var.reset(token)
