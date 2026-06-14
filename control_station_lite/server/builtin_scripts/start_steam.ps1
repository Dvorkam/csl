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

# Launch the Steam client. Resolves the install path from the registry and
# falls back to the steam:// protocol handler. Not persistent — this fires Steam
# off and returns; the agent does not supervise the Steam process.
$ErrorActionPreference = 'Stop'

$steamExe = (Get-ItemProperty -Path 'HKCU:\Software\Valve\Steam' `
    -Name 'SteamExe' -ErrorAction SilentlyContinue).SteamExe

if ($steamExe -and (Test-Path -LiteralPath $steamExe)) {
    Start-Process -FilePath $steamExe
} else {
    # Protocol handler — works whenever Steam is installed for the user.
    Start-Process 'steam://open/main'
}
