# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration test: multi-step approval CLI flow without the control station.

Exercises the full state-machine progression through the CLI:
  absent → pending → approved → update_pending → approved → absent

All filesystem operations are redirected to tmp_path; no agent process is
started and no network calls are made.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from control_station_lite.agent.cli import main

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_env(tmp_path: Path) -> dict[str, Path]:
    """Set up a minimal agent filesystem and return a dict of key paths."""
    base = tmp_path / ".csl"
    agent_dir = base / "agent"
    scripts_dir = base / "scripts"
    pending_dir = base / "scripts.pending"
    logs_dir = base / "logs"

    for d in (agent_dir, scripts_dir, pending_dir, logs_dir):
        d.mkdir(parents=True, exist_ok=True)

    approvals_path = agent_dir / "approvals.json"
    approvals_path.write_text(json.dumps({"scripts": {}}), encoding="utf-8")

    config_path = base / "config.yaml"
    config_data = {
        "agent": {
            "listen_port": 36717,
            "idle_timeout_seconds": 600,
            "lifecycle_check_interval_seconds": 10,
            "log_tail_lines": 1000,
            "scripts_dir": str(scripts_dir),
            "pending_dir": str(pending_dir),
            "logs_dir": str(logs_dir),
            "state_path": str(agent_dir / "running.json"),
            "approvals_path": str(approvals_path),
        },
        "identity": {"key_fingerprint": "SHA256:test", "hostname_hint": "testhost"},
        "approval_policy": {"auto_approve": []},
        "advanced": {
            "windows_admin_authorized_keys_path": (
                "C:/ProgramData/ssh/administrators_authorized_keys"
            )
        },
    }
    config_path.write_text(yaml.dump(config_data), encoding="utf-8")

    return {
        "base": base,
        "approvals": approvals_path,
        "scripts": scripts_dir,
        "pending": pending_dir,
        "config": config_path,
    }


def _run(argv: list[str], config_path: Path) -> None:
    with (
        patch("sys.argv", ["csl-agent"] + argv),
        patch(
            "control_station_lite.agent.cli.cmd_policy.default_config_path",
            return_value=config_path,
        ),
        patch(
            "control_station_lite.agent.config.default_config_path",
            return_value=config_path,
        ),
    ):
        main()


def _read_approvals(env: dict[str, Path]) -> dict:  # type: ignore[type-arg]
    return json.loads(env["approvals"].read_text(encoding="utf-8"))["scripts"]


# ---------------------------------------------------------------------------
# Happy-path flow: absent → pending → approved → absent
# ---------------------------------------------------------------------------


class TestApprovalFlowHappyPath:
    def test_pending_to_approved(
        self,
        agent_env: dict[str, Path],
        capsys: pytest.CaptureFixture,  # type: ignore[type-arg]
    ) -> None:
        env = agent_env
        # Put a script in pending state
        script_content = "#!/bin/bash\necho hello world\n"
        (env["pending"] / "my_script").write_text(script_content, encoding="utf-8")
        env["approvals"].write_text(
            json.dumps({"scripts": {"my_script": {"state": "pending", "pending_md5": "abc123"}}}),
            encoding="utf-8",
        )

        # list — should show PENDING
        _run(["approvals", "list"], env["config"])
        out = capsys.readouterr().out
        assert "my_script" in out
        assert "PENDING" in out

        # show — should print script content
        _run(["approvals", "show", "my_script"], env["config"])
        out = capsys.readouterr().out
        assert "echo hello world" in out

        # approve
        _run(["approvals", "approve", "my_script"], env["config"])
        capsys.readouterr()

        # verify state in file
        scripts = _read_approvals(env)
        assert scripts["my_script"]["state"] == "approved"
        # script moved from pending → scripts dir
        assert (env["scripts"] / "my_script").exists()

        # list — should now show approved (no PENDING badge)
        _run(["approvals", "list"], env["config"])
        out = capsys.readouterr().out
        assert "my_script" in out
        assert "PENDING" not in out

        # clear — remove from approvals
        _run(["approvals", "clear", "my_script"], env["config"])
        capsys.readouterr()

        scripts = _read_approvals(env)
        assert "my_script" not in scripts

        # list — empty
        _run(["approvals", "list"], env["config"])
        out = capsys.readouterr().out
        assert "No scripts" in out

    def test_pending_to_rejected_to_clear(
        self,
        agent_env: dict[str, Path],
        capsys: pytest.CaptureFixture,  # type: ignore[type-arg]
    ) -> None:
        env = agent_env
        (env["pending"] / "bad_script").write_text("rm -rf /", encoding="utf-8")
        env["approvals"].write_text(
            json.dumps({"scripts": {"bad_script": {"state": "pending", "pending_md5": "bad"}}}),
            encoding="utf-8",
        )

        _run(["approvals", "reject", "bad_script"], env["config"])
        capsys.readouterr()

        scripts = _read_approvals(env)
        assert scripts["bad_script"]["state"] == "rejected"

        _run(["approvals", "clear", "bad_script"], env["config"])
        capsys.readouterr()

        assert "bad_script" not in _read_approvals(env)


# ---------------------------------------------------------------------------
# Update flow: approved → update_pending → re-approved
# ---------------------------------------------------------------------------


class TestApprovalFlowUpdate:
    def test_update_pending_to_reapproved(
        self,
        agent_env: dict[str, Path],
        capsys: pytest.CaptureFixture,  # type: ignore[type-arg]
    ) -> None:
        env = agent_env
        old_content = "#!/bin/bash\necho old\n"
        new_content = "#!/bin/bash\necho new\n"

        (env["scripts"] / "s").write_text(old_content, encoding="utf-8")
        (env["pending"] / "s").write_text(new_content, encoding="utf-8")
        env["approvals"].write_text(
            json.dumps(
                {
                    "scripts": {
                        "s": {
                            "state": "update_pending",
                            "approved_md5": "old_md5",
                            "pending_md5": "new_md5",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        # diff — should show lines removed/added
        _run(["approvals", "diff", "s"], env["config"])
        out = capsys.readouterr().out
        assert "-echo old" in out
        assert "+echo new" in out

        # approve
        _run(["approvals", "approve", "s"], env["config"])
        capsys.readouterr()

        scripts = _read_approvals(env)
        assert scripts["s"]["state"] == "approved"
        assert (env["scripts"] / "s").read_text(encoding="utf-8") == new_content


# ---------------------------------------------------------------------------
# Policy flow: auto-approve → manual
# ---------------------------------------------------------------------------


class TestPolicyFlow:
    def test_add_then_remove_auto_approve(
        self,
        agent_env: dict[str, Path],
        capsys: pytest.CaptureFixture,  # type: ignore[type-arg]
    ) -> None:
        env = agent_env

        # Initially empty
        _run(["policy", "show"], env["config"])
        assert "empty" in capsys.readouterr().out.lower()

        # Add safe_script to auto-approve
        _run(["policy", "auto-approve", "safe_script"], env["config"])
        capsys.readouterr()

        config_data = yaml.safe_load(env["config"].read_text(encoding="utf-8"))
        assert "safe_script" in config_data["approval_policy"]["auto_approve"]

        # Show — should list it
        _run(["policy", "show"], env["config"])
        out = capsys.readouterr().out
        assert "safe_script" in out

        # Remove
        _run(["policy", "manual", "safe_script"], env["config"])
        capsys.readouterr()

        config_data = yaml.safe_load(env["config"].read_text(encoding="utf-8"))
        assert "safe_script" not in config_data["approval_policy"]["auto_approve"]

        # Show — empty again
        _run(["policy", "show"], env["config"])
        assert "empty" in capsys.readouterr().out.lower()

    def test_idempotent_auto_approve(
        self,
        agent_env: dict[str, Path],
        capsys: pytest.CaptureFixture,  # type: ignore[type-arg]
    ) -> None:
        env = agent_env
        _run(["policy", "auto-approve", "s"], env["config"])
        capsys.readouterr()
        _run(["policy", "auto-approve", "s"], env["config"])
        out = capsys.readouterr().out
        assert "already" in out

        config_data = yaml.safe_load(env["config"].read_text(encoding="utf-8"))
        assert config_data["approval_policy"]["auto_approve"].count("s") == 1


# ---------------------------------------------------------------------------
# Error / invalid-transition paths
# ---------------------------------------------------------------------------


class TestInvalidTransitions:
    def test_approve_already_approved_exits_1(self, agent_env: dict[str, Path]) -> None:
        env = agent_env
        env["approvals"].write_text(
            json.dumps({"scripts": {"s": {"state": "approved", "approved_md5": "x"}}}),
            encoding="utf-8",
        )
        with pytest.raises(SystemExit) as exc:
            _run(["approvals", "approve", "s"], env["config"])
        assert exc.value.code == 1

    def test_reject_already_approved_exits_1(self, agent_env: dict[str, Path]) -> None:
        env = agent_env
        env["approvals"].write_text(
            json.dumps({"scripts": {"s": {"state": "approved", "approved_md5": "x"}}}),
            encoding="utf-8",
        )
        with pytest.raises(SystemExit) as exc:
            _run(["approvals", "reject", "s"], env["config"])
        assert exc.value.code == 1

    def test_clear_absent_exits_1(self, agent_env: dict[str, Path]) -> None:
        env = agent_env
        with pytest.raises(SystemExit) as exc:
            _run(["approvals", "clear", "ghost"], env["config"])
        assert exc.value.code == 1

    def test_show_approved_not_pending_exits_1(self, agent_env: dict[str, Path]) -> None:
        env = agent_env
        env["approvals"].write_text(
            json.dumps({"scripts": {"s": {"state": "approved", "approved_md5": "x"}}}),
            encoding="utf-8",
        )
        with pytest.raises(SystemExit) as exc:
            _run(["approvals", "show", "s"], env["config"])
        assert exc.value.code == 1

    def test_diff_pending_not_update_pending_exits_1(self, agent_env: dict[str, Path]) -> None:
        env = agent_env
        (env["pending"] / "s").write_text("content", encoding="utf-8")
        env["approvals"].write_text(
            json.dumps({"scripts": {"s": {"state": "pending", "pending_md5": "x"}}}),
            encoding="utf-8",
        )
        with pytest.raises(SystemExit) as exc:
            _run(["approvals", "diff", "s"], env["config"])
        assert exc.value.code == 1
