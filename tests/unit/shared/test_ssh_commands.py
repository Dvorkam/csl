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

"""Tests for the centralised SSH command allowlist."""

from __future__ import annotations

import pytest

from control_station_lite.shared import ssh_commands
from control_station_lite.shared.ssh_commands import (
    CONFIG_READ_CMD,
    WAKEUP_CMD,
    allowed_commands,
    current_platform_name,
)


class TestAllowedCommands:
    @pytest.mark.parametrize("platform", ["linux", "windows", "macos"])
    def test_contains_wakeup_and_config_read(self, platform: str) -> None:
        allowed = allowed_commands(platform)
        assert WAKEUP_CMD[platform] in allowed
        assert CONFIG_READ_CMD[platform] in allowed

    def test_exactly_two_commands_per_platform(self) -> None:
        # linux/macos share the same config-read string, so the set has 2 entries.
        assert len(allowed_commands("linux")) == 2

    def test_unknown_platform_is_empty(self) -> None:
        assert allowed_commands("solaris") == frozenset()

    def test_returns_frozenset(self) -> None:
        assert isinstance(allowed_commands("linux"), frozenset)


class TestCurrentPlatformName:
    def test_returns_known_value(self) -> None:
        assert current_platform_name() in {"linux", "windows", "macos"}

    def test_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ssh_commands, "IS_WINDOWS", True)
        monkeypatch.setattr(ssh_commands, "IS_MACOS", False)
        assert current_platform_name() == "windows"

    def test_macos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ssh_commands, "IS_WINDOWS", False)
        monkeypatch.setattr(ssh_commands, "IS_MACOS", True)
        assert current_platform_name() == "macos"

    def test_linux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ssh_commands, "IS_WINDOWS", False)
        monkeypatch.setattr(ssh_commands, "IS_MACOS", False)
        assert current_platform_name() == "linux"
