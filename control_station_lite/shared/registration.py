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

"""Registration bundle encode/decode.

A registration bundle is a base64-encoded JSON blob that the target owner
passes to the control station admin when registering a new machine.  It
carries everything the control station needs to establish an SSH connection
and talk to the agent:

  private_key    — OpenSSH PEM-encoded Ed25519 private key (string)
  key_fingerprint — SHA256 fingerprint (e.g. "SHA256:abc…")
  api_token      — bearer token the control station presents to the agent (string)
  agent_port     — TCP port the agent listens on (int)
  scripts_dir    — absolute path to approved scripts on the target (string)
  hostname_hint  — human-readable hostname, advisory only (string)
  platform       — "linux" | "windows" | "macos" (string)
  agent_version  — the agent's package version (string); the control station
                   refuses registration on a major-version mismatch (§11)

Decoding (Task 4.1) will live here once the server side is implemented.
"""

from __future__ import annotations

import base64
import json
from typing import Any

__all__ = ["RegistrationBundle", "encode_bundle"]

# Keys present in every valid bundle.
_REQUIRED_FIELDS = frozenset(
    {
        "private_key",
        "key_fingerprint",
        "api_token",
        "agent_port",
        "scripts_dir",
        "hostname_hint",
        "platform",
        "ssh_user",
        "agent_version",
    }
)

_VALID_PLATFORMS = frozenset({"linux", "windows", "macos"})


class RegistrationBundle:
    """Parsed, validated registration bundle."""

    def __init__(
        self,
        private_key: str,
        key_fingerprint: str,
        agent_port: int,
        scripts_dir: str,
        hostname_hint: str,
        platform: str,
        ssh_user: str,
        api_token: str,
        agent_version: str,
    ) -> None:
        self.private_key = private_key
        self.key_fingerprint = key_fingerprint
        self.agent_port = agent_port
        self.scripts_dir = scripts_dir
        self.hostname_hint = hostname_hint
        self.platform = platform
        self.ssh_user = ssh_user
        self.api_token = api_token
        self.agent_version = agent_version

    def encode(self) -> str:
        """Return the base64-encoded JSON representation."""
        return encode_bundle(
            private_key=self.private_key,
            key_fingerprint=self.key_fingerprint,
            agent_port=self.agent_port,
            scripts_dir=self.scripts_dir,
            hostname_hint=self.hostname_hint,
            platform=self.platform,
            ssh_user=self.ssh_user,
            api_token=self.api_token,
            agent_version=self.agent_version,
        )

    @classmethod
    def decode(cls, bundle: str) -> RegistrationBundle:
        """Decode and validate a base64-encoded bundle string.

        Raises:
            ValueError: if the bundle is not valid base64, not valid JSON,
                missing required fields, or contains an unknown platform.
        """
        try:
            raw = base64.b64decode(bundle.encode(), validate=True)
        except Exception as exc:
            raise ValueError(f"bundle is not valid base64: {exc}") from exc
        try:
            data: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"bundle JSON is malformed: {exc}") from exc

        missing = _REQUIRED_FIELDS - data.keys()
        if missing:
            raise ValueError(f"bundle missing fields: {sorted(missing)}")

        platform = data["platform"]
        if platform not in _VALID_PLATFORMS:
            raise ValueError(
                f"unknown platform {platform!r}; expected one of {sorted(_VALID_PLATFORMS)}"
            )

        return cls(
            private_key=data["private_key"],
            key_fingerprint=data["key_fingerprint"],
            agent_port=int(data["agent_port"]),
            scripts_dir=data["scripts_dir"],
            hostname_hint=data["hostname_hint"],
            platform=platform,
            ssh_user=data["ssh_user"],
            api_token=data["api_token"],
            agent_version=data["agent_version"],
        )


def encode_bundle(
    *,
    private_key: str,
    key_fingerprint: str,
    agent_port: int,
    scripts_dir: str,
    hostname_hint: str,
    platform: str,
    ssh_user: str,
    api_token: str,
    agent_version: str,
) -> str:
    """Return a base64-encoded JSON registration bundle string."""
    payload: dict[str, Any] = {
        "private_key": private_key,
        "key_fingerprint": key_fingerprint,
        "agent_port": agent_port,
        "scripts_dir": scripts_dir,
        "hostname_hint": hostname_hint,
        "platform": platform,
        "ssh_user": ssh_user,
        "api_token": api_token,
        "agent_version": agent_version,
    }
    return base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
