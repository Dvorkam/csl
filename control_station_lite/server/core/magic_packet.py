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

"""Wake-on-LAN Magic Packet: build and broadcast."""

import re
import socket

_OCTET = r"([0-9A-Fa-f]{2})"
_SEP = r"[:\-]?"
_MAC_RE = re.compile(r"^" + (_OCTET + _SEP) * 5 + _OCTET + r"$")


def build_packet(mac: str) -> bytes:
    """Return a 102-byte Magic Packet for *mac*.

    Accepts MAC addresses in any of these forms:
      AA:BB:CC:DD:EE:FF  (colon-separated)
      AA-BB-CC-DD-EE-FF  (hyphen-separated)
      AABBCCDDEEFF       (no separator)

    Raises ValueError on invalid format.
    """
    m = _MAC_RE.match(mac.strip())
    if not m:
        raise ValueError(f"Invalid MAC address: {mac!r}")
    mac_bytes = bytes(int(g, 16) for g in m.groups())
    return b"\xff" * 6 + mac_bytes * 16


def broadcast(
    mac: str,
    broadcast_addr: str = "255.255.255.255",
    port: int = 9,
) -> None:
    """Send a Wake-on-LAN Magic Packet for *mac* to *broadcast_addr*:*port* over UDP."""
    packet = build_packet(mac)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (broadcast_addr, port))
