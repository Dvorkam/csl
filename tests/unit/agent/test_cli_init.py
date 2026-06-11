"""Tests for `csl-agent init` (agent/cli.py).

All filesystem operations are redirected to tmp_path.  service_installer
is mocked so the tests don't touch systemd/schtasks.
"""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from control_station_lite.agent.cli import main
from control_station_lite.agent.cli.cmd_init import (
    _append_authorized_keys,
    _build_authorized_keys_entry,
    _generate_keypair,
    _platform_name,
    _ssh_fingerprint,
    _write_approvals,
    _write_config,
)
from control_station_lite.agent.cli.cmd_setup import (
    ReadinessIssue,
    _check_readiness_linux,
    _check_readiness_macos,
    _check_readiness_windows,
    _sshd_running_linux,
    _sshd_running_macos,
    _sshd_running_windows,
    _windows_is_admin,
    check_readiness,
)

# ---------------------------------------------------------------------------
# SSH key helpers
# ---------------------------------------------------------------------------


class TestGenerateKeypair:
    def test_creates_private_and_public_key(self, tmp_path: Path) -> None:
        keys_dir = tmp_path / "keys"
        priv, fp, pub = _generate_keypair(keys_dir)

        assert (keys_dir / "csl_ed25519").exists()
        assert (keys_dir / "csl_ed25519.pub").exists()

    def test_private_key_is_pem_string(self, tmp_path: Path) -> None:
        priv, fp, pub = _generate_keypair(tmp_path / "keys")
        assert priv.startswith("-----BEGIN OPENSSH PRIVATE KEY-----")

    def test_fingerprint_format(self, tmp_path: Path) -> None:
        _, fp, _ = _generate_keypair(tmp_path / "keys")
        assert fp.startswith("SHA256:")
        assert len(fp) > 10

    def test_public_key_openssh_format(self, tmp_path: Path) -> None:
        _, _, pub = _generate_keypair(tmp_path / "keys")
        assert pub.decode().startswith("ssh-ed25519 ")

    def test_public_key_has_comment(self, tmp_path: Path) -> None:
        _, _, pub = _generate_keypair(tmp_path / "keys")
        parts = pub.decode().strip().split()
        assert len(parts) == 3, f"expected type+key+comment, got {parts}"

    def test_comment_is_csl_agent_at_hostname(self, tmp_path: Path) -> None:
        _, _, pub = _generate_keypair(tmp_path / "keys")
        comment = pub.decode().strip().split()[2]
        assert comment == f"csl-agent@{socket.gethostname()}"

    def test_fingerprint_unaffected_by_comment(self, tmp_path: Path) -> None:
        _, fp, pub = _generate_keypair(tmp_path / "keys")
        assert fp == _ssh_fingerprint(pub)

    def test_idempotent_reuses_existing_key(self, tmp_path: Path) -> None:
        keys_dir = tmp_path / "keys"
        priv1, fp1, pub1 = _generate_keypair(keys_dir)
        priv2, fp2, pub2 = _generate_keypair(keys_dir)
        assert priv1 == priv2
        assert fp1 == fp2
        assert pub1 == pub2


class TestSshFingerprint:
    def test_fingerprint_matches_generate(self, tmp_path: Path) -> None:
        _, fp_from_gen, pub = _generate_keypair(tmp_path / "keys")
        fp_from_pub = _ssh_fingerprint(pub)
        assert fp_from_gen == fp_from_pub

    def test_sha256_prefix(self, tmp_path: Path) -> None:
        _, _, pub = _generate_keypair(tmp_path / "keys")
        assert _ssh_fingerprint(pub).startswith("SHA256:")

    def test_two_different_keys_have_different_fingerprints(self, tmp_path: Path) -> None:
        _, _, pub1 = _generate_keypair(tmp_path / "k1")
        _, _, pub2 = _generate_keypair(tmp_path / "k2")
        assert _ssh_fingerprint(pub1) != _ssh_fingerprint(pub2)


# ---------------------------------------------------------------------------
# authorized_keys helper
# ---------------------------------------------------------------------------


class TestAppendAuthorizedKeys:
    def test_creates_file_if_absent(self, tmp_path: Path) -> None:
        _, _, pub = _generate_keypair(tmp_path / "keys")
        ak = tmp_path / ".ssh" / "authorized_keys"

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("control_station_lite.agent.cli.cmd_init._windows_is_admin", return_value=False),
        ):
            _append_authorized_keys(pub)

        assert ak.exists()
        assert pub.decode().strip() in ak.read_text()

    def test_appends_to_existing_file(self, tmp_path: Path) -> None:
        _, _, pub = _generate_keypair(tmp_path / "keys")
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        ak = ssh_dir / "authorized_keys"
        ak.write_text("ssh-rsa EXISTING_KEY user@host\n")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("control_station_lite.agent.cli.cmd_init._windows_is_admin", return_value=False),
        ):
            _append_authorized_keys(pub)

        content = ak.read_text()
        assert "EXISTING_KEY" in content
        assert pub.decode().strip() in content

    def test_idempotent_does_not_duplicate(self, tmp_path: Path) -> None:
        _, _, pub = _generate_keypair(tmp_path / "keys")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("control_station_lite.agent.cli.cmd_init._windows_is_admin", return_value=False),
        ):
            _append_authorized_keys(pub)
            _append_authorized_keys(pub)

        content = (tmp_path / ".ssh" / "authorized_keys").read_text()
        assert content.count(pub.decode().strip()) == 1


# ---------------------------------------------------------------------------
# Forced-command restriction (task 8.5.1)
# ---------------------------------------------------------------------------


class TestBuildAuthorizedKeysEntry:
    def test_includes_forced_gateway_command(self, tmp_path: Path) -> None:
        _, _, pub = _generate_keypair(tmp_path / "keys")
        entry = _build_authorized_keys_entry(pub, 36717)
        assert 'command="' in entry
        assert "ssh-gateway" in entry

    def test_includes_restrict_and_permitopen(self, tmp_path: Path) -> None:
        _, _, pub = _generate_keypair(tmp_path / "keys")
        entry = _build_authorized_keys_entry(pub, 40000)
        assert "restrict" in entry
        assert "port-forwarding" in entry
        assert 'permitopen="127.0.0.1:40000"' in entry

    def test_ends_with_public_key_line(self, tmp_path: Path) -> None:
        _, _, pub = _generate_keypair(tmp_path / "keys")
        entry = _build_authorized_keys_entry(pub, 36717)
        assert entry.endswith(pub.decode().strip())

    def test_port_is_honoured(self, tmp_path: Path) -> None:
        _, _, pub = _generate_keypair(tmp_path / "keys")
        assert 'permitopen="127.0.0.1:12345"' in _build_authorized_keys_entry(pub, 12345)


class TestAppendAuthorizedKeysRestricted:
    def test_written_entry_is_restricted(self, tmp_path: Path) -> None:
        _, _, pub = _generate_keypair(tmp_path / "keys")
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("control_station_lite.agent.cli.cmd_init._windows_is_admin", return_value=False),
        ):
            _append_authorized_keys(pub, agent_port=36717)

        content = (tmp_path / ".ssh" / "authorized_keys").read_text()
        assert 'command="' in content
        assert "ssh-gateway" in content
        assert 'permitopen="127.0.0.1:36717"' in content

    def test_upgrades_old_bare_key_entry(self, tmp_path: Path) -> None:
        # Simulate an authorized_keys written by an older, unrestricted init.
        _, _, pub = _generate_keypair(tmp_path / "keys")
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        ak = ssh_dir / "authorized_keys"
        ak.write_text(pub.decode().strip() + "\n")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("control_station_lite.agent.cli.cmd_init._windows_is_admin", return_value=False),
        ):
            _append_authorized_keys(pub, agent_port=36717)

        content = ak.read_text()
        key_body = pub.decode().split()[1]
        # Key appears exactly once, now carrying the restriction.
        assert content.count(key_body) == 1
        assert "ssh-gateway" in content

    def test_preserves_unrelated_keys_on_upgrade(self, tmp_path: Path) -> None:
        _, _, pub = _generate_keypair(tmp_path / "keys")
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        ak = ssh_dir / "authorized_keys"
        ak.write_text("ssh-rsa OTHER_KEY someone@elsewhere\n")

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("control_station_lite.agent.cli.cmd_init._windows_is_admin", return_value=False),
        ):
            _append_authorized_keys(pub, agent_port=36717)

        content = ak.read_text()
        assert "OTHER_KEY" in content
        assert "ssh-gateway" in content

    def test_idempotent_across_reruns(self, tmp_path: Path) -> None:
        _, _, pub = _generate_keypair(tmp_path / "keys")
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("control_station_lite.agent.cli.cmd_init._windows_is_admin", return_value=False),
        ):
            _append_authorized_keys(pub, agent_port=36717)
            _append_authorized_keys(pub, agent_port=36717)

        content = (tmp_path / ".ssh" / "authorized_keys").read_text()
        assert content.count(pub.decode().split()[1]) == 1


# ---------------------------------------------------------------------------
# _windows_is_admin
# ---------------------------------------------------------------------------


class TestWindowsIsAdmin:
    def test_returns_false_on_non_windows(self) -> None:
        if sys.platform != "win32":
            assert _windows_is_admin() is False

    def test_returns_bool(self) -> None:
        result = _windows_is_admin()
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# authorized_keys — Windows Administrator path
# ---------------------------------------------------------------------------


class TestAppendAuthorizedKeysWindowsAdmin:
    """Tests for the Windows Administrator authorized_keys code path.

    All tests mock IS_WINDOWS=True and _windows_is_admin=True so the branch
    runs on Linux CI, and redirect _WINDOWS_ADMIN_AK_PATH to tmp_path.
    """

    def test_writes_to_admin_path(self, tmp_path: Path) -> None:
        _, _, pub = _generate_keypair(tmp_path / "keys")
        admin_ak = tmp_path / "ProgramData" / "ssh" / "administrators_authorized_keys"

        with (
            patch("control_station_lite.agent.cli.cmd_init.IS_WINDOWS", True),
            patch("control_station_lite.agent.cli.cmd_init._windows_is_admin", return_value=True),
            patch("control_station_lite.agent.cli.cmd_init._WINDOWS_ADMIN_AK_PATH", admin_ak),
            patch("control_station_lite.agent.cli.cmd_init._set_admin_ak_acl"),
        ):
            _append_authorized_keys(pub)

        assert admin_ak.exists()
        assert pub.decode().strip() in admin_ak.read_text()

    def test_does_not_write_to_user_path(self, tmp_path: Path) -> None:
        _, _, pub = _generate_keypair(tmp_path / "keys")
        admin_ak = tmp_path / "ProgramData" / "ssh" / "administrators_authorized_keys"

        with (
            patch("control_station_lite.agent.cli.cmd_init.IS_WINDOWS", True),
            patch("control_station_lite.agent.cli.cmd_init._windows_is_admin", return_value=True),
            patch("control_station_lite.agent.cli.cmd_init._WINDOWS_ADMIN_AK_PATH", admin_ak),
            patch("control_station_lite.agent.cli.cmd_init._set_admin_ak_acl"),
        ):
            _append_authorized_keys(pub)

        assert not (tmp_path / ".ssh" / "authorized_keys").exists()

    def test_calls_set_admin_acl(self, tmp_path: Path) -> None:
        _, _, pub = _generate_keypair(tmp_path / "keys")
        admin_ak = tmp_path / "ProgramData" / "ssh" / "administrators_authorized_keys"

        with (
            patch("control_station_lite.agent.cli.cmd_init.IS_WINDOWS", True),
            patch("control_station_lite.agent.cli.cmd_init._windows_is_admin", return_value=True),
            patch("control_station_lite.agent.cli.cmd_init._WINDOWS_ADMIN_AK_PATH", admin_ak),
            patch("control_station_lite.agent.cli.cmd_init._set_admin_ak_acl") as mock_acl,
        ):
            _append_authorized_keys(pub)

        mock_acl.assert_called_once_with(admin_ak)

    def test_idempotent_on_admin_path(self, tmp_path: Path) -> None:
        _, _, pub = _generate_keypair(tmp_path / "keys")
        admin_ak = tmp_path / "ProgramData" / "ssh" / "administrators_authorized_keys"

        with (
            patch("control_station_lite.agent.cli.cmd_init.IS_WINDOWS", True),
            patch("control_station_lite.agent.cli.cmd_init._windows_is_admin", return_value=True),
            patch("control_station_lite.agent.cli.cmd_init._WINDOWS_ADMIN_AK_PATH", admin_ak),
            patch("control_station_lite.agent.cli.cmd_init._set_admin_ak_acl"),
        ):
            _append_authorized_keys(pub)
            _append_authorized_keys(pub)

        assert admin_ak.read_text().count(pub.decode().strip()) == 1

    def test_non_admin_windows_uses_user_path(self, tmp_path: Path) -> None:
        _, _, pub = _generate_keypair(tmp_path / "keys")

        with (
            patch("control_station_lite.agent.cli.cmd_init.IS_WINDOWS", True),
            patch("control_station_lite.agent.cli.cmd_init._windows_is_admin", return_value=False),
            patch("pathlib.Path.home", return_value=tmp_path),
        ):
            _append_authorized_keys(pub)

        ak = tmp_path / ".ssh" / "authorized_keys"
        assert ak.exists()
        assert pub.decode().strip() in ak.read_text()


# ---------------------------------------------------------------------------
# Config / state helpers
# ---------------------------------------------------------------------------


class TestWriteConfig:
    def test_creates_yaml_file(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        with patch(
            "control_station_lite.agent.cli.cmd_init.CslPaths.platform_base",
            return_value=tmp_path,
        ):
            _write_config(config_path, "SHA256:abc", 47731, "tok-abc")

        assert config_path.exists()

    def test_yaml_has_agent_section(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        with patch(
            "control_station_lite.agent.cli.cmd_init.CslPaths.platform_base",
            return_value=tmp_path,
        ):
            _write_config(config_path, "SHA256:abc", 47731, "tok-abc")

        data = yaml.safe_load(config_path.read_text())
        assert data["agent"]["listen_port"] == 47731
        assert data["identity"]["key_fingerprint"] == "SHA256:abc"
        assert data["identity"]["api_token"] == "tok-abc"

    def test_custom_port(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        with patch(
            "control_station_lite.agent.cli.cmd_init.CslPaths.platform_base",
            return_value=tmp_path,
        ):
            _write_config(config_path, "SHA256:xyz", 9000, "tok-xyz")

        data = yaml.safe_load(config_path.read_text())
        assert data["agent"]["listen_port"] == 9000

    def test_approval_policy_auto_approve_empty(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        with patch(
            "control_station_lite.agent.cli.cmd_init.CslPaths.platform_base",
            return_value=tmp_path,
        ):
            _write_config(config_path, "SHA256:abc", 47731, "tok-abc")

        data = yaml.safe_load(config_path.read_text())
        assert data["approval_policy"]["auto_approve"] == []


class TestWriteApprovals:
    def test_creates_empty_approvals(self, tmp_path: Path) -> None:
        approvals_path = tmp_path / "agent" / "approvals.json"
        _write_approvals(approvals_path)
        data = json.loads(approvals_path.read_text())
        assert data == {"scripts": {}}

    def test_does_not_overwrite_existing(self, tmp_path: Path) -> None:
        approvals_path = tmp_path / "approvals.json"
        approvals_path.write_text('{"scripts": {"existing": {}}}')
        _write_approvals(approvals_path)
        data = json.loads(approvals_path.read_text())
        assert "existing" in data["scripts"]


# ---------------------------------------------------------------------------
# Platform name
# ---------------------------------------------------------------------------


class TestPlatformName:
    def test_returns_string(self) -> None:
        name = _platform_name()
        assert name in {"linux", "windows", "macos"}

    def test_linux_on_linux(self) -> None:
        with (
            patch("control_station_lite.agent.cli.cmd_init.IS_WINDOWS", False),
            patch("control_station_lite.agent.cli.cmd_init.IS_MACOS", False),
        ):
            assert _platform_name() == "linux"

    def test_windows(self) -> None:
        with patch("control_station_lite.agent.cli.cmd_init.IS_WINDOWS", True):
            assert _platform_name() == "windows"

    def test_macos(self) -> None:
        with (
            patch("control_station_lite.agent.cli.cmd_init.IS_WINDOWS", False),
            patch("control_station_lite.agent.cli.cmd_init.IS_MACOS", True),
        ):
            assert _platform_name() == "macos"


# ---------------------------------------------------------------------------
# Full init command (end-to-end with mocks)
# ---------------------------------------------------------------------------


class TestCmdInit:
    def _run_init(self, tmp_path: Path, extra_args: list[str] | None = None) -> None:
        """Run `csl-agent init` with all filesystem ops rooted at tmp_path."""
        args = ["csl-agent", "init"] + (extra_args or [])
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "control_station_lite.agent.cli.cmd_init.CslPaths.platform_base",
                return_value=tmp_path / ".csl",
            ),
            patch(
                "control_station_lite.agent.cli.cmd_init.default_config_path",
                return_value=tmp_path / ".csl" / "config.yaml",
            ),
            patch("control_station_lite.agent.cli.cmd_init.install_service"),
            # Always use the user-level authorized_keys path in tests so they
            # work regardless of whether the CI runner is an Administrator.
            patch("control_station_lite.agent.cli.cmd_init._windows_is_admin", return_value=False),
            patch("sys.argv", args),
        ):
            main()

    def test_creates_directory_structure(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:  # type: ignore[type-arg]
        self._run_init(tmp_path)
        base = tmp_path / ".csl"
        assert (base / "scripts").is_dir()
        assert (base / "scripts.pending").is_dir()
        assert (base / "logs").is_dir()
        assert (base / "keys").is_dir()

    def test_creates_keypair(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:  # type: ignore[type-arg]
        self._run_init(tmp_path)
        keys_dir = tmp_path / ".csl" / "keys"
        assert (keys_dir / "csl_ed25519").exists()
        assert (keys_dir / "csl_ed25519.pub").exists()

    def test_creates_authorized_keys(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:  # type: ignore[type-arg]
        self._run_init(tmp_path)
        ak = tmp_path / ".ssh" / "authorized_keys"
        assert ak.exists()
        assert "ssh-ed25519" in ak.read_text()

    def test_creates_config_yaml(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:  # type: ignore[type-arg]
        self._run_init(tmp_path)
        config_path = tmp_path / ".csl" / "config.yaml"
        assert config_path.exists()
        data = yaml.safe_load(config_path.read_text())
        assert data["agent"]["listen_port"] == 36717
        assert "lifecycle_check_interval_seconds" in data["agent"]
        assert "log_tail_lines" in data["agent"]
        assert "advanced" in data
        assert "windows_admin_authorized_keys_path" in data["advanced"]

    def test_creates_approvals_json(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:  # type: ignore[type-arg]
        self._run_init(tmp_path)
        approvals = tmp_path / ".csl" / "agent" / "approvals.json"
        assert approvals.exists()
        assert json.loads(approvals.read_text()) == {"scripts": {}}

    def test_calls_install_service(self, tmp_path: Path) -> None:
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "control_station_lite.agent.cli.cmd_init.CslPaths.platform_base",
                return_value=tmp_path / ".csl",
            ),
            patch(
                "control_station_lite.agent.cli.cmd_init.default_config_path",
                return_value=tmp_path / ".csl" / "config.yaml",
            ),
            patch("control_station_lite.agent.cli.cmd_init.install_service") as mock_install,
            patch("sys.argv", ["csl-agent", "init"]),
        ):
            main()
        mock_install.assert_called_once()

    def test_prints_registration_bundle(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,  # type: ignore[type-arg]
    ) -> None:
        self._run_init(tmp_path)
        out = capsys.readouterr().out
        assert "REGISTRATION BUNDLE" in out
        # Find the bundle line and decode it
        lines = out.strip().splitlines()
        bundle_line = next(
            (ln for ln in lines if ln and not ln.startswith("=") and not ln.startswith("Agent")),
            None,
        )
        assert bundle_line is not None
        from control_station_lite.shared.registration import RegistrationBundle

        bundle = RegistrationBundle.decode(bundle_line)
        assert bundle.agent_port == 36717
        assert bundle.key_fingerprint.startswith("SHA256:")
        assert bundle.platform in {"linux", "windows", "macos"}

    def test_custom_port(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:  # type: ignore[type-arg]
        self._run_init(tmp_path, ["--port", "9999"])
        out = capsys.readouterr().out
        lines = out.strip().splitlines()
        bundle_line = next(
            (ln for ln in lines if ln and not ln.startswith("=") and not ln.startswith("Agent")),
            None,
        )
        assert bundle_line is not None
        from control_station_lite.shared.registration import RegistrationBundle

        bundle = RegistrationBundle.decode(bundle_line)
        assert bundle.agent_port == 9999

    def test_idempotent_second_run(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:  # type: ignore[type-arg]
        self._run_init(tmp_path)
        out1 = capsys.readouterr().out

        self._run_init(tmp_path)
        out2 = capsys.readouterr().out

        # Both runs should produce a bundle
        assert "REGISTRATION BUNDLE" in out1
        assert "REGISTRATION BUNDLE" in out2

        # Fingerprint must be the same (key reused)
        from control_station_lite.shared.registration import RegistrationBundle

        def _extract_bundle(out: str) -> RegistrationBundle:
            lines = out.strip().splitlines()
            line = next(
                ln for ln in lines if ln and not ln.startswith("=") and not ln.startswith("Agent")
            )
            return RegistrationBundle.decode(line)

        b1 = _extract_bundle(out1)
        b2 = _extract_bundle(out2)
        assert b1.key_fingerprint == b2.key_fingerprint

    def test_service_install_failure_is_non_fatal(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,  # type: ignore[type-arg]
    ) -> None:
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "control_station_lite.agent.cli.cmd_init.CslPaths.platform_base",
                return_value=tmp_path / ".csl",
            ),
            patch(
                "control_station_lite.agent.cli.cmd_init.default_config_path",
                return_value=tmp_path / ".csl" / "config.yaml",
            ),
            patch(
                "control_station_lite.agent.cli.cmd_init.install_service",
                side_effect=RuntimeError("daemon not running"),
            ),
            patch("sys.argv", ["csl-agent", "init"]),
        ):
            main()  # must not raise

        out = capsys.readouterr().out
        assert "REGISTRATION BUNDLE" in out


# ---------------------------------------------------------------------------
# Readiness checks — Linux
# ---------------------------------------------------------------------------


class TestCheckReadinessLinux:
    def test_all_ok_when_sshd_installed_and_running(self) -> None:
        with (
            patch(
                "control_station_lite.agent.cli.cmd_setup.shutil.which",
                return_value="/usr/sbin/sshd",
            ),
            patch(
                "control_station_lite.agent.cli.cmd_setup._sshd_running_linux", return_value=True
            ),
        ):
            assert _check_readiness_linux() == []

    def test_error_when_sshd_not_installed(self) -> None:
        with patch("control_station_lite.agent.cli.cmd_setup.shutil.which", return_value=None):
            issues = _check_readiness_linux()
        assert len(issues) == 1
        assert issues[0].severity == "error"
        assert "not installed" in issues[0].description.lower()

    def test_warning_when_sshd_not_running(self) -> None:
        with (
            patch(
                "control_station_lite.agent.cli.cmd_setup.shutil.which",
                return_value="/usr/sbin/sshd",
            ),
            patch(
                "control_station_lite.agent.cli.cmd_setup._sshd_running_linux", return_value=False
            ),
        ):
            issues = _check_readiness_linux()
        assert len(issues) == 1
        assert issues[0].severity == "warning"
        assert "not running" in issues[0].description.lower()

    def test_no_running_check_when_not_installed(self) -> None:
        with (
            patch("control_station_lite.agent.cli.cmd_setup.shutil.which", return_value=None),
            patch("control_station_lite.agent.cli.cmd_setup._sshd_running_linux") as mock_running,
        ):
            _check_readiness_linux()
        mock_running.assert_not_called()

    def test_fix_hint_mentions_systemctl(self) -> None:
        with (
            patch(
                "control_station_lite.agent.cli.cmd_setup.shutil.which",
                return_value="/usr/sbin/sshd",
            ),
            patch(
                "control_station_lite.agent.cli.cmd_setup._sshd_running_linux", return_value=False
            ),
        ):
            issues = _check_readiness_linux()
        assert "systemctl" in issues[0].fix_hint


class TestSshdRunningLinux:
    def test_returns_true_when_systemctl_succeeds(self) -> None:
        with patch(
            "control_station_lite.agent.cli.cmd_setup.subprocess.run",
            return_value=MagicMock(returncode=0),
        ):
            assert _sshd_running_linux() is True

    def test_returns_false_when_all_checks_fail(self) -> None:
        with patch(
            "control_station_lite.agent.cli.cmd_setup.subprocess.run",
            return_value=MagicMock(returncode=1),
        ):
            assert _sshd_running_linux() is False


# ---------------------------------------------------------------------------
# Readiness checks — Windows
# ---------------------------------------------------------------------------


class TestCheckReadinessWindows:
    def test_error_when_sshd_exe_missing(self, tmp_path: Path) -> None:
        fake_exe = tmp_path / "sshd.exe"  # does not exist
        with patch(
            "control_station_lite.agent.cli.cmd_setup._sshd_exe_windows", return_value=fake_exe
        ):
            issues = _check_readiness_windows()
        assert len(issues) == 1
        assert issues[0].severity == "error"

    def test_warning_when_service_not_running(self, tmp_path: Path) -> None:
        fake_exe = tmp_path / "sshd.exe"
        fake_exe.touch()
        with (
            patch(
                "control_station_lite.agent.cli.cmd_setup._sshd_exe_windows", return_value=fake_exe
            ),
            patch(
                "control_station_lite.agent.cli.cmd_setup._sshd_running_windows", return_value=False
            ),
            patch("control_station_lite.agent.cli.cmd_setup._windows_is_admin", return_value=False),
        ):
            issues = _check_readiness_windows()
        assert len(issues) == 1
        assert issues[0].severity == "warning"
        assert "not running" in issues[0].description.lower()

    def test_all_ok_when_installed_and_running(self, tmp_path: Path) -> None:
        fake_exe = tmp_path / "sshd.exe"
        fake_exe.touch()
        with (
            patch(
                "control_station_lite.agent.cli.cmd_setup._sshd_exe_windows", return_value=fake_exe
            ),
            patch(
                "control_station_lite.agent.cli.cmd_setup._sshd_running_windows", return_value=True
            ),
            patch("control_station_lite.agent.cli.cmd_setup._windows_is_admin", return_value=False),
        ):
            assert _check_readiness_windows() == []

    def test_fix_hint_mentions_powershell_install(self, tmp_path: Path) -> None:
        fake_exe = tmp_path / "sshd.exe"  # missing
        with patch(
            "control_station_lite.agent.cli.cmd_setup._sshd_exe_windows", return_value=fake_exe
        ):
            issues = _check_readiness_windows()
        assert "Add-WindowsCapability" in issues[0].fix_hint


class TestSshdRunningWindows:
    def test_returns_true_when_running_in_output(self) -> None:
        with patch(
            "control_station_lite.agent.cli.cmd_setup.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="STATE: 4 RUNNING"),
        ):
            assert _sshd_running_windows() is True

    def test_returns_false_when_not_running(self) -> None:
        with patch(
            "control_station_lite.agent.cli.cmd_setup.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="STATE: 1 STOPPED"),
        ):
            assert _sshd_running_windows() is False


# ---------------------------------------------------------------------------
# Readiness checks — macOS
# ---------------------------------------------------------------------------


class TestCheckReadinessMacos:
    def test_all_ok_when_sshd_running(self) -> None:
        with patch(
            "control_station_lite.agent.cli.cmd_setup._sshd_running_macos", return_value=True
        ):
            assert _check_readiness_macos() == []

    def test_warning_when_sshd_not_running(self) -> None:
        with patch(
            "control_station_lite.agent.cli.cmd_setup._sshd_running_macos", return_value=False
        ):
            issues = _check_readiness_macos()
        assert len(issues) == 1
        assert issues[0].severity == "warning"
        assert "remote login" in issues[0].description.lower()

    def test_fix_hint_mentions_systemsetup(self) -> None:
        with patch(
            "control_station_lite.agent.cli.cmd_setup._sshd_running_macos", return_value=False
        ):
            issues = _check_readiness_macos()
        assert "systemsetup" in issues[0].fix_hint


class TestSshdRunningMacos:
    def test_returns_true_when_launchctl_succeeds(self) -> None:
        with patch(
            "control_station_lite.agent.cli.cmd_setup.subprocess.run",
            return_value=MagicMock(returncode=0),
        ):
            assert _sshd_running_macos() is True

    def test_returns_false_when_both_checks_fail(self) -> None:
        with patch(
            "control_station_lite.agent.cli.cmd_setup.subprocess.run",
            return_value=MagicMock(returncode=1),
        ):
            assert _sshd_running_macos() is False


# ---------------------------------------------------------------------------
# check_readiness dispatch
# ---------------------------------------------------------------------------


class TestCheckReadinessDispatch:
    def test_dispatches_to_linux(self) -> None:
        with (
            patch("control_station_lite.agent.cli.cmd_setup.IS_WINDOWS", False),
            patch("control_station_lite.agent.cli.cmd_setup.IS_MACOS", False),
            patch("control_station_lite.agent.cli.cmd_setup.IS_LINUX", True),
            patch(
                "control_station_lite.agent.cli.cmd_setup._check_readiness_linux", return_value=[]
            ) as mock,
        ):
            check_readiness()
        mock.assert_called_once()

    def test_dispatches_to_windows(self) -> None:
        with (
            patch("control_station_lite.agent.cli.cmd_setup.IS_WINDOWS", True),
            patch(
                "control_station_lite.agent.cli.cmd_setup._check_readiness_windows", return_value=[]
            ) as mock,
        ):
            check_readiness()
        mock.assert_called_once()

    def test_dispatches_to_macos(self) -> None:
        with (
            patch("control_station_lite.agent.cli.cmd_setup.IS_WINDOWS", False),
            patch("control_station_lite.agent.cli.cmd_setup.IS_MACOS", True),
            patch(
                "control_station_lite.agent.cli.cmd_setup._check_readiness_macos", return_value=[]
            ) as mock,
        ):
            check_readiness()
        mock.assert_called_once()


# ---------------------------------------------------------------------------
# _cmd_init prints readiness warnings
# ---------------------------------------------------------------------------


class TestCmdInitReadinessIntegration:
    def _run_init(self, tmp_path: Path) -> None:
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch(
                "control_station_lite.agent.cli.cmd_init.CslPaths.platform_base",
                return_value=tmp_path / ".csl",
            ),
            patch(
                "control_station_lite.agent.cli.cmd_init.default_config_path",
                return_value=tmp_path / ".csl" / "config.yaml",
            ),
            patch("control_station_lite.agent.cli.cmd_init.install_service"),
            patch("sys.argv", ["csl-agent", "init"]),
        ):
            main()

    def test_init_prints_warning_when_sshd_not_running(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,  # type: ignore[type-arg]
    ) -> None:
        with patch(
            "control_station_lite.agent.cli.cmd_init.check_readiness",
            return_value=[
                ReadinessIssue(
                    "warning", "SSH daemon is not running.", "sudo systemctl enable --now ssh"
                )
            ],
        ):
            self._run_init(tmp_path)

        err = capsys.readouterr().err
        assert "[WARN]" in err
        assert "SSH daemon" in err

    def test_init_prints_error_and_setup_hint_on_error(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,  # type: ignore[type-arg]
    ) -> None:
        with patch(
            "control_station_lite.agent.cli.cmd_init.check_readiness",
            return_value=[
                ReadinessIssue("error", "sshd not installed.", "sudo apt install openssh-server")
            ],
        ):
            self._run_init(tmp_path)

        err = capsys.readouterr().err
        assert "[ERROR]" in err
        assert "csl-agent setup" in err

    def test_init_continues_despite_errors(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,  # type: ignore[type-arg]
    ) -> None:
        with patch(
            "control_station_lite.agent.cli.cmd_init.check_readiness",
            return_value=[ReadinessIssue("error", "sshd not installed.", "install hint")],
        ):
            self._run_init(tmp_path)  # must not raise or sys.exit

        assert "REGISTRATION BUNDLE" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# csl-agent setup subcommand
# ---------------------------------------------------------------------------


class TestCmdSetup:
    def _run_setup(self) -> None:
        with patch("sys.argv", ["csl-agent", "setup"]):
            main()

    def test_setup_prints_all_clear_when_no_issues(
        self,
        capsys: pytest.CaptureFixture,  # type: ignore[type-arg]
    ) -> None:
        with patch("control_station_lite.agent.cli.cmd_setup.check_readiness", return_value=[]):
            self._run_setup()
        assert "all prerequisite checks passed" in capsys.readouterr().out.lower()

    def test_setup_runs_setup_system_when_issues_exist(self) -> None:
        with (
            patch(
                "control_station_lite.agent.cli.cmd_setup.check_readiness",
                side_effect=[
                    [ReadinessIssue("warning", "sshd not running", "fix hint")],
                    [],  # re-check after fix: all clear
                ],
            ),
            patch("control_station_lite.agent.cli.cmd_setup.setup_system") as mock_setup,
        ):
            self._run_setup()
        mock_setup.assert_called_once()

    def test_setup_exits_1_when_issues_remain(self) -> None:
        issue = ReadinessIssue("error", "sshd not installed", "install it")
        with (
            patch(
                "control_station_lite.agent.cli.cmd_setup.check_readiness",
                side_effect=[[issue], [issue]],  # still broken after fix attempt
            ),
            patch("control_station_lite.agent.cli.cmd_setup.setup_system"),
            pytest.raises(SystemExit) as exc_info,
        ):
            self._run_setup()
        assert exc_info.value.code == 1
