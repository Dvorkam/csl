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

# Put the machine to sleep (suspend-to-RAM). Run via `bash`, so no shebang.
# May require the agent user to have suspend rights (logind on Linux normally
# grants this to an active session).
# shellcheck shell=bash
set -eu

case "$(uname -s)" in
    Linux)
        systemctl suspend
        ;;
    Darwin)
        pmset sleepnow
        ;;
    *)
        echo "sleep_machine: unsupported OS '$(uname -s)'" >&2
        exit 1
        ;;
esac
