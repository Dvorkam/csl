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

# Reboot the machine. Run via `bash`, so no shebang. Requires the agent user to
# have reboot rights (root / a polkit rule); otherwise the command is refused by
# the OS and the job exits non-zero.
# shellcheck shell=bash
set -eu

case "$(uname -s)" in
    Linux)
        systemctl reboot
        ;;
    Darwin)
        shutdown -r now
        ;;
    *)
        echo "restart_machine: unsupported OS '$(uname -s)'" >&2
        exit 1
        ;;
esac
