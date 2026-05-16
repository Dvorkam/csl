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

from __future__ import annotations

import platform

__all__ = ["CURRENT_PLATFORM", "IS_LINUX", "IS_MACOS", "IS_WINDOWS"]

CURRENT_PLATFORM: str = platform.system()

IS_LINUX: bool = CURRENT_PLATFORM == "Linux"
IS_WINDOWS: bool = CURRENT_PLATFORM == "Windows"
IS_MACOS: bool = CURRENT_PLATFORM == "Darwin"
