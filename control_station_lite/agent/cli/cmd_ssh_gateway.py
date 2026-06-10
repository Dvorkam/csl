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

"""csl-agent ssh-gateway — forced command for the control station's SSH key.

The control station's dedicated key is installed in ``authorized_keys`` with
``command="csl-agent ssh-gateway"``, so every exec request the control station
makes arrives here as ``$SSH_ORIGINAL_COMMAND`` instead of running a shell.
This subcommand runs the request only if it exactly matches one of the
allowlisted commands for this platform (see ``shared.ssh_commands``); anything
else — including an empty command, i.e. an interactive shell attempt — is
refused. Port forwarding to the agent is permitted separately by the
``permitopen`` restriction on the key, not by this gateway.
"""

from __future__ import annotations

import logging
import os
import subprocess

from control_station_lite.shared.ssh_commands import allowed_commands, current_platform_name

logger = logging.getLogger(__name__)

# Exit code used when a request is refused (mirrors a shell "permission denied").
_REJECTED_EXIT = 126


def cmd_ssh_gateway() -> int:
    """Run an allowlisted SSH command from ``$SSH_ORIGINAL_COMMAND``.

    Returns the wrapped command's exit code, or ``126`` if the request is not
    permitted.
    """
    requested = os.environ.get("SSH_ORIGINAL_COMMAND", "")
    allowed = allowed_commands(current_platform_name())

    if requested not in allowed:
        logger.warning("ssh-gateway: refused command %r", requested)
        print("csl-agent ssh-gateway: command not permitted", flush=True)
        return _REJECTED_EXIT

    # Run the vetted constant (identical to the request, but using the
    # allowlisted literal makes the safety guarantee explicit). A shell is
    # required so that ``~`` and ``$UID`` in the allowlisted commands expand.
    matched = next(cmd for cmd in allowed if cmd == requested)
    logger.info("ssh-gateway: executing %r", matched)
    proc = subprocess.run(matched, shell=True)  # noqa: S602 — only ever a vetted constant
    return proc.returncode
