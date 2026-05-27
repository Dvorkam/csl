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

"""Unit tests for server/core/magic_packet.py."""

import socket
from unittest.mock import MagicMock, patch

import pytest

from control_station_lite.server.core.magic_packet import broadcast, build_packet


class TestBuildPacket:
    def test_colon_separated_mac(self) -> None:
        pkt = build_packet("AA:BB:CC:DD:EE:FF")
        assert len(pkt) == 102
        assert pkt[:6] == b"\xff" * 6
        assert pkt[6:12] == bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF])
        # MAC repeated 16 times after the header
        mac = bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF])
        assert pkt[6:] == mac * 16

    def test_hyphen_separated_mac(self) -> None:
        pkt = build_packet("AA-BB-CC-DD-EE-FF")
        assert len(pkt) == 102
        assert pkt[6:12] == bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF])

    def test_no_separator_mac(self) -> None:
        pkt = build_packet("AABBCCDDEEFF")
        assert len(pkt) == 102
        assert pkt[6:12] == bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF])

    def test_lowercase_mac(self) -> None:
        pkt = build_packet("aa:bb:cc:dd:ee:ff")
        assert pkt[6:12] == bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF])

    def test_mixed_case_mac(self) -> None:
        pkt = build_packet("Aa:Bb:Cc:Dd:Ee:Ff")
        assert pkt[6:12] == bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF])

    def test_all_zeros_mac(self) -> None:
        pkt = build_packet("00:00:00:00:00:00")
        assert pkt == b"\xff" * 6 + b"\x00" * 96

    def test_broadcast_mac(self) -> None:
        pkt = build_packet("FF:FF:FF:FF:FF:FF")
        assert pkt == b"\xff" * 102

    def test_invalid_mac_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Invalid MAC address"):
            build_packet("ZZ:ZZ:ZZ:ZZ:ZZ:ZZ")

    def test_too_short_mac_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Invalid MAC address"):
            build_packet("AA:BB:CC:DD:EE")

    def test_too_long_mac_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Invalid MAC address"):
            build_packet("AA:BB:CC:DD:EE:FF:00")

    def test_empty_string_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Invalid MAC address"):
            build_packet("")

    def test_whitespace_stripped(self) -> None:
        pkt = build_packet("  AA:BB:CC:DD:EE:FF  ")
        assert len(pkt) == 102


class TestBroadcast:
    def _make_mock_sock(self) -> MagicMock:
        mock_sock = MagicMock()
        mock_sock.__enter__ = MagicMock(return_value=mock_sock)
        mock_sock.__exit__ = MagicMock(return_value=False)
        return mock_sock

    def test_sends_udp_broadcast(self) -> None:
        mock_sock = self._make_mock_sock()
        with patch(
            "control_station_lite.server.core.magic_packet.socket.socket", return_value=mock_sock
        ):
            broadcast("AA:BB:CC:DD:EE:FF")

        mock_sock.setsockopt.assert_called_once_with(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        args, _ = mock_sock.sendto.call_args
        packet, addr = args
        assert len(packet) == 102
        assert addr == ("255.255.255.255", 9)

    def test_custom_broadcast_addr_and_port(self) -> None:
        mock_sock = self._make_mock_sock()
        with patch(
            "control_station_lite.server.core.magic_packet.socket.socket", return_value=mock_sock
        ):
            broadcast("AA:BB:CC:DD:EE:FF", broadcast_addr="192.168.1.255", port=7)

        _, addr = mock_sock.sendto.call_args[0]
        assert addr == ("192.168.1.255", 7)

    def test_invalid_mac_propagates_value_error(self) -> None:
        with pytest.raises(ValueError, match="Invalid MAC address"):
            broadcast("not-a-mac")

    def test_socket_created_as_udp(self) -> None:
        mock_sock = self._make_mock_sock()
        with patch(
            "control_station_lite.server.core.magic_packet.socket.socket",
            return_value=mock_sock,
        ) as mock_socket_cls:
            broadcast("AA:BB:CC:DD:EE:FF")

        mock_socket_cls.assert_called_once_with(socket.AF_INET, socket.SOCK_DGRAM)
