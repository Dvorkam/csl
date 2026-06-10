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

"""Tests for ``csl-agent ssh-gateway`` — the forced-command allowlist."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from control_station_lite.agent.cli import cmd_ssh_gateway as gw
from control_station_lite.shared.ssh_commands import WAKEUP_CMD


@pytest.fixture
def _linux_platform() -> None:
    # Pin the gateway to the linux allowlist regardless of the host running tests.
    with patch.object(gw, "current_platform_name", return_value="linux"):
        yield


@pytest.mark.usefixtures("_linux_platform")
class TestSshGateway:
    def test_allowed_command_runs_and_returns_exit_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SSH_ORIGINAL_COMMAND", WAKEUP_CMD["linux"])
        with patch.object(gw.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args="", returncode=0)
            rc = gw.cmd_ssh_gateway()
        assert rc == 0
        # The vetted constant is what gets executed, via shell.
        args, kwargs = mock_run.call_args
        assert args[0] == WAKEUP_CMD["linux"]
        assert kwargs["shell"] is True

    def test_allowed_command_propagates_nonzero_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SSH_ORIGINAL_COMMAND", WAKEUP_CMD["linux"])
        with patch.object(gw.subprocess, "run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args="", returncode=3)
            assert gw.cmd_ssh_gateway() == 3

    def test_disallowed_command_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "rm -rf /")
        with patch.object(gw.subprocess, "run") as mock_run:
            rc = gw.cmd_ssh_gateway()
        assert rc == gw._REJECTED_EXIT
        mock_run.assert_not_called()

    def test_empty_command_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SSH_ORIGINAL_COMMAND", raising=False)
        with patch.object(gw.subprocess, "run") as mock_run:
            rc = gw.cmd_ssh_gateway()
        assert rc == gw._REJECTED_EXIT
        mock_run.assert_not_called()

    def test_command_with_extra_args_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Exact-match only: appending to an allowed command must not slip through.
        monkeypatch.setenv("SSH_ORIGINAL_COMMAND", WAKEUP_CMD["linux"] + " && rm -rf /")
        with patch.object(gw.subprocess, "run") as mock_run:
            rc = gw.cmd_ssh_gateway()
        assert rc == gw._REJECTED_EXIT
        mock_run.assert_not_called()
