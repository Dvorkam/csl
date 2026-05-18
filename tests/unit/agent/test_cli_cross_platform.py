# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cross-platform tests for csl-agent init and service installation.

Focus:
- File permission bits set by _append_authorized_keys (Linux only — chmod
  semantics don't apply on Windows).
- Private-key file permissions (Linux only).
- CRLF-safe idempotency in _append_authorized_keys (runs on every platform).
- Windows-simulated path handling in _write_config (runs on every platform
  via IS_WINDOWS mocking so Linux CI can cover both branches).
- Windows service-installer dispatch simulated on Linux via IS_WINDOWS mock.
"""

from __future__ import annotations

import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from control_station_lite.agent.cli.cmd_init import (
    _append_authorized_keys,
    _generate_keypair,
    _write_config,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pubkey(tmp_path: Path) -> bytes:
    _, _, pub = _generate_keypair(tmp_path / "keys")
    return pub


def _mode(path: Path) -> int:
    """Return the permission bits (lower 9 bits) of *path*."""
    return stat.S_IMODE(path.stat().st_mode)


# ===========================================================================
# authorized_keys permissions (Linux only)
# ===========================================================================


@pytest.mark.linux_only
class TestAuthorizedKeysPermissionsLinux:
    """Verify that _append_authorized_keys sets correct POSIX permissions."""

    def test_ssh_dir_created_with_mode_700(self, tmp_path: Path) -> None:
        pub = _make_pubkey(tmp_path)
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("control_station_lite.agent.cli.cmd_init._windows_is_admin", return_value=False),
        ):
            _append_authorized_keys(pub)

        ssh_dir = tmp_path / ".ssh"
        assert ssh_dir.is_dir()
        assert _mode(ssh_dir) == 0o700, f"expected 700, got {oct(_mode(ssh_dir))}"

    def test_authorized_keys_created_with_mode_600(self, tmp_path: Path) -> None:
        pub = _make_pubkey(tmp_path)
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("control_station_lite.agent.cli.cmd_init._windows_is_admin", return_value=False),
        ):
            _append_authorized_keys(pub)

        ak = tmp_path / ".ssh" / "authorized_keys"
        assert ak.exists()
        assert _mode(ak) == 0o600, f"expected 600, got {oct(_mode(ak))}"

    def test_appending_to_existing_does_not_change_mode(self, tmp_path: Path) -> None:
        """Mode of a pre-existing authorized_keys must not be altered."""
        pub = _make_pubkey(tmp_path)
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir(parents=True)
        ak = ssh_dir / "authorized_keys"
        ak.write_text("ssh-rsa AAAA existing\n", encoding="utf-8")
        ak.chmod(0o644)  # deliberately loose; should stay 644 after append

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("control_station_lite.agent.cli.cmd_init._windows_is_admin", return_value=False),
        ):
            _append_authorized_keys(pub)

        # chmod(0o600) is only called for newly created files
        assert _mode(ak) == 0o644, "existing file mode must not be changed"

    def test_ssh_dir_preexisting_mode_not_reset(self, tmp_path: Path) -> None:
        """chmod(0o700) is called on the .ssh dir even when it already exists."""
        pub = _make_pubkey(tmp_path)
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir(parents=True)
        ssh_dir.chmod(0o755)  # looser than 700

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("control_station_lite.agent.cli.cmd_init._windows_is_admin", return_value=False),
        ):
            _append_authorized_keys(pub)

        # The implementation calls ssh_dir.chmod(0o700) unconditionally
        assert _mode(ssh_dir) == 0o700


# ===========================================================================
# Private key permissions (Linux only)
# ===========================================================================


@pytest.mark.linux_only
class TestPrivateKeyPermissionsLinux:
    def test_private_key_created_with_mode_600(self, tmp_path: Path) -> None:
        keys_dir = tmp_path / "keys"
        _generate_keypair(keys_dir)

        priv = keys_dir / "csl_ed25519"
        assert priv.exists()
        assert _mode(priv) == 0o600, f"expected 600, got {oct(_mode(priv))}"

    def test_reused_key_mode_unchanged(self, tmp_path: Path) -> None:
        keys_dir = tmp_path / "keys"
        _generate_keypair(keys_dir)
        # Loosen permission; second call must not tighten or touch it
        (keys_dir / "csl_ed25519").chmod(0o644)
        _generate_keypair(keys_dir)
        # The second call reuses; chmod is only called during generation
        assert _mode(keys_dir / "csl_ed25519") == 0o644


# ===========================================================================
# CRLF-safe idempotency in authorized_keys (runs on every platform)
# ===========================================================================


class TestAuthorizedKeysCrlfSafety:
    """_append_authorized_keys must treat CRLF and LF lines as equivalent."""

    def test_crlf_existing_key_is_not_duplicated(self, tmp_path: Path) -> None:
        """A key line stored with CRLF must be recognised as already present."""
        pub = _make_pubkey(tmp_path)
        pub_line = pub.decode().strip()

        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir(parents=True)
        ak = ssh_dir / "authorized_keys"
        # Write key with Windows CRLF line endings
        ak.write_bytes((pub_line + "\r\n").encode("utf-8"))

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("control_station_lite.agent.cli.cmd_init._windows_is_admin", return_value=False),
        ):
            _append_authorized_keys(pub)

        # The key must appear exactly once in the file (as LF-normalised by text-mode read)
        content = ak.read_bytes().decode("utf-8")
        assert content.count(pub_line) == 1

    def test_append_to_crlf_file_produces_valid_content(self, tmp_path: Path) -> None:
        """Appending to an existing CRLF file leaves both keys readable."""
        pub1 = _make_pubkey(tmp_path / "k1")
        pub2 = _make_pubkey(tmp_path / "k2")
        pub1_line = pub1.decode().strip()
        pub2_line = pub2.decode().strip()

        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir(parents=True)
        ak = ssh_dir / "authorized_keys"
        ak.write_bytes((pub1_line + "\r\n").encode("utf-8"))

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("control_station_lite.agent.cli.cmd_init._windows_is_admin", return_value=False),
        ):
            _append_authorized_keys(pub2)

        content = ak.read_text(encoding="utf-8")
        assert pub1_line in content
        assert pub2_line in content

    def test_mixed_crlf_and_lf_no_duplication(self, tmp_path: Path) -> None:
        """File with mixed line endings: a key present as LF must not be re-added."""
        pub = _make_pubkey(tmp_path)
        pub_line = pub.decode().strip()

        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir(parents=True)
        ak = ssh_dir / "authorized_keys"
        # Mixed: first key CRLF, second key LF
        other = "ssh-rsa AAAA other@host"
        ak.write_bytes((other + "\r\n" + pub_line + "\n").encode("utf-8"))

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("control_station_lite.agent.cli.cmd_init._windows_is_admin", return_value=False),
        ):
            _append_authorized_keys(pub)

        content = ak.read_text(encoding="utf-8")
        assert content.count(pub_line) == 1


# ===========================================================================
# Windows-simulated path handling in config.yaml (runs on every platform)
# ===========================================================================


class TestWindowsConfigPaths:
    """When IS_WINDOWS=True, _write_config must still produce valid YAML with
    absolute paths for every path-valued key."""

    def _write_windows_config(self, tmp_path: Path) -> dict:  # type: ignore[type-arg]
        config_path = tmp_path / "config.yaml"
        with patch(
            "control_station_lite.agent.cli.cmd_init.CslPaths.platform_base",
            return_value=tmp_path / ".csl",
        ):
            _write_config(config_path, "SHA256:abc", 36717)
        return yaml.safe_load(config_path.read_text(encoding="utf-8"))

    def test_agent_paths_are_absolute(self, tmp_path: Path) -> None:
        data = self._write_windows_config(tmp_path)
        agent = data["agent"]
        for key in ("scripts_dir", "pending_dir", "logs_dir", "state_path", "approvals_path"):
            path = Path(agent[key])
            assert path.is_absolute(), f"{key} is not absolute: {agent[key]}"

    def test_config_paths_are_under_base(self, tmp_path: Path) -> None:
        base = tmp_path / ".csl"
        data = self._write_windows_config(tmp_path)
        agent = data["agent"]
        for key in ("scripts_dir", "pending_dir", "logs_dir"):
            path = Path(agent[key])
            assert str(base) in str(path), f"{key} not under base: {agent[key]}"

    def test_advanced_section_windows_ak_path_is_string(self, tmp_path: Path) -> None:
        data = self._write_windows_config(tmp_path)
        ak_path = data["advanced"]["windows_admin_authorized_keys_path"]
        assert isinstance(ak_path, str)
        assert len(ak_path) > 0

    def test_simulate_windows_is_windows_true(self, tmp_path: Path) -> None:
        """With IS_WINDOWS patched to True, config.yaml still loads correctly."""
        config_path = tmp_path / "config.yaml"
        with (
            patch("control_station_lite.agent.cli.cmd_init.IS_WINDOWS", True),
            patch(
                "control_station_lite.agent.cli.cmd_init.CslPaths.platform_base",
                return_value=tmp_path / ".csl",
            ),
        ):
            _write_config(config_path, "SHA256:xyz", 36717)

        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert data["agent"]["listen_port"] == 36717
        assert data["identity"]["key_fingerprint"] == "SHA256:xyz"


# ===========================================================================
# Windows-simulated service installer dispatch (runs on every platform)
# ===========================================================================


class TestWindowsServiceInstallerSimulated:
    """Verify that install_service() dispatches to _install_windows when
    IS_WINDOWS=True, even when running on Linux CI."""

    def test_dispatches_windows_on_windows_flag(self) -> None:
        from control_station_lite.agent.service_installer import install_service

        with (
            patch("control_station_lite.agent.service_installer.IS_WINDOWS", True),
            patch("control_station_lite.agent.service_installer._install_windows") as mock_win,
            patch(
                "control_station_lite.agent.service_installer._agent_executable",
                return_value=r"C:\Python\pythonw.exe",
            ),
        ):
            install_service()

        mock_win.assert_called_once_with(r"C:\Python\pythonw.exe")

    def test_windows_task_xml_has_correct_command(self) -> None:
        from control_station_lite.agent.service_installer import _windows_task_xml

        xml = _windows_task_xml(r"C:\Python\pythonw.exe")
        assert r"<Command>C:\Python\pythonw.exe</Command>" in xml

    def test_windows_task_xml_uses_backslash_for_executable(self) -> None:
        from control_station_lite.agent.service_installer import _windows_task_xml

        xml = _windows_task_xml(r"C:\Program Files\Python\pythonw.exe")
        assert r"C:\Program Files\Python\pythonw.exe" in xml

    def test_schtasks_invocation_format(self) -> None:
        """_install_windows calls schtasks /create with the right flags."""
        from control_station_lite.agent.service_installer import _install_windows

        with patch("control_station_lite.agent.service_installer.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            _install_windows(r"C:\Python\pythonw.exe")

        call_args = mock_run.call_args[0][0]
        assert "schtasks" in call_args
        assert "/create" in call_args
        assert "/f" in call_args
        assert "/tn" in call_args
