"""Tests for `csl-agent approvals` and `csl-agent policy` subcommands."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from control_station_lite.agent.cli import main

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_paths(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """Return (approvals_path, scripts_dir, pending_dir, config_path)."""
    base = tmp_path / ".csl"
    approvals = base / "agent" / "approvals.json"
    scripts = base / "scripts"
    pending = base / "scripts.pending"
    config = base / "config.yaml"
    for d in (scripts, pending, base / "agent", base / "logs"):
        d.mkdir(parents=True, exist_ok=True)
    return approvals, scripts, pending, config


def _write_approvals(approvals_path: Path, data: dict) -> None:  # type: ignore[type-arg]
    approvals_path.write_text(json.dumps({"scripts": data}, indent=2), encoding="utf-8")


def _write_config(config_path: Path, base: Path, auto_approve: list[str] | None = None) -> None:
    data = {
        "agent": {
            "listen_port": 47731,
            "idle_timeout_seconds": 600,
            "scripts_dir": str(base / "scripts"),
            "pending_dir": str(base / "scripts.pending"),
            "logs_dir": str(base / "logs"),
            "state_path": str(base / "agent" / "running.json"),
            "approvals_path": str(base / "agent" / "approvals.json"),
        },
        "identity": {"key_fingerprint": "SHA256:test", "hostname_hint": "test"},
        "approval_policy": {"auto_approve": auto_approve or []},
    }
    config_path.write_text(yaml.dump(data), encoding="utf-8")


def _run(argv: list[str], tmp_path: Path, auto_approve: list[str] | None = None) -> None:
    """Run `csl-agent <argv>` with filesystem rooted at tmp_path."""
    base = tmp_path / ".csl"
    config_path = base / "config.yaml"
    _write_config(config_path, base, auto_approve)
    # Patch both the cli module reference (used by _cmd_policy_set_entry) and
    # the config module reference (used by load_config() inside _load_approvals).
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


# ---------------------------------------------------------------------------
# approvals list
# ---------------------------------------------------------------------------


class TestApprovalsList:
    def test_empty_prints_no_scripts_message(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,  # type: ignore[type-arg]
    ) -> None:
        approvals, _, _, _ = _make_paths(tmp_path)
        _write_approvals(approvals, {})
        _run(["approvals", "list"], tmp_path)
        assert "No scripts" in capsys.readouterr().out

    def test_lists_script_names(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,  # type: ignore[type-arg]
    ) -> None:
        approvals, _, _, _ = _make_paths(tmp_path)
        _write_approvals(
            approvals,
            {
                "alpha": {"state": "approved", "approved_md5": "aaa"},
                "beta": {"state": "pending", "pending_md5": "bbb"},
            },
        )
        _run(["approvals", "list"], tmp_path)
        out = capsys.readouterr().out
        assert "alpha" in out
        assert "beta" in out

    def test_shows_state_labels(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,  # type: ignore[type-arg]
    ) -> None:
        approvals, _, _, _ = _make_paths(tmp_path)
        _write_approvals(approvals, {"my_script": {"state": "pending", "pending_md5": "abc"}})
        _run(["approvals", "list"], tmp_path)
        assert "PENDING" in capsys.readouterr().out

    def test_shows_update_pending_label(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,  # type: ignore[type-arg]
    ) -> None:
        approvals, _, _, _ = _make_paths(tmp_path)
        _write_approvals(
            approvals,
            {"s": {"state": "update_pending", "approved_md5": "old", "pending_md5": "new"}},
        )
        _run(["approvals", "list"], tmp_path)
        assert "UPDATE PENDING" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# approvals show
# ---------------------------------------------------------------------------


class TestApprovalsShow:
    def test_prints_pending_content(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,  # type: ignore[type-arg]
    ) -> None:
        approvals, _, pending, _ = _make_paths(tmp_path)
        _write_approvals(approvals, {"my_script": {"state": "pending", "pending_md5": "abc"}})
        (pending / "my_script").write_text("#!/bin/bash\necho hello\n", encoding="utf-8")
        _run(["approvals", "show", "my_script"], tmp_path)
        assert "echo hello" in capsys.readouterr().out

    def test_exits_1_when_not_pending(self, tmp_path: Path) -> None:
        approvals, _, _, _ = _make_paths(tmp_path)
        _write_approvals(approvals, {"s": {"state": "approved", "approved_md5": "abc"}})
        with pytest.raises(SystemExit) as exc_info:
            _run(["approvals", "show", "s"], tmp_path)
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# approvals diff
# ---------------------------------------------------------------------------


class TestApprovalsDiff:
    def test_prints_diff_for_update_pending(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,  # type: ignore[type-arg]
    ) -> None:
        approvals, scripts, pending, _ = _make_paths(tmp_path)
        _write_approvals(
            approvals,
            {"s": {"state": "update_pending", "approved_md5": "old", "pending_md5": "new"}},
        )
        (scripts / "s").write_text("line one\nline two\n", encoding="utf-8")
        (pending / "s").write_text("line one\nline three\n", encoding="utf-8")
        _run(["approvals", "diff", "s"], tmp_path)
        out = capsys.readouterr().out
        assert "-line two" in out
        assert "+line three" in out

    def test_exits_1_when_not_update_pending(self, tmp_path: Path) -> None:
        approvals, _, pending, _ = _make_paths(tmp_path)
        _write_approvals(approvals, {"s": {"state": "pending", "pending_md5": "abc"}})
        (pending / "s").write_text("content", encoding="utf-8")
        with pytest.raises(SystemExit) as exc_info:
            _run(["approvals", "diff", "s"], tmp_path)
        assert exc_info.value.code == 1

    def test_identical_files_prints_identical(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,  # type: ignore[type-arg]
    ) -> None:
        approvals, scripts, pending, _ = _make_paths(tmp_path)
        _write_approvals(
            approvals,
            {"s": {"state": "update_pending", "approved_md5": "x", "pending_md5": "x"}},
        )
        (scripts / "s").write_text("same\n", encoding="utf-8")
        (pending / "s").write_text("same\n", encoding="utf-8")
        _run(["approvals", "diff", "s"], tmp_path)
        assert "identical" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# approvals approve
# ---------------------------------------------------------------------------


class TestApprovalsApprove:
    def test_approve_pending_script(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,  # type: ignore[type-arg]
    ) -> None:
        approvals, scripts, pending, _ = _make_paths(tmp_path)
        _write_approvals(approvals, {"s": {"state": "pending", "pending_md5": "abc"}})
        (pending / "s").write_text("#!/bin/bash\n", encoding="utf-8")
        _run(["approvals", "approve", "s"], tmp_path)
        assert "approved" in capsys.readouterr().out.lower()
        assert (scripts / "s").exists()
        data = json.loads(approvals.read_text())
        assert data["scripts"]["s"]["state"] == "approved"

    def test_approve_wrong_state_exits_1(self, tmp_path: Path) -> None:
        approvals, _, _, _ = _make_paths(tmp_path)
        _write_approvals(approvals, {"s": {"state": "approved", "approved_md5": "abc"}})
        with pytest.raises(SystemExit) as exc_info:
            _run(["approvals", "approve", "s"], tmp_path)
        assert exc_info.value.code == 1

    def test_approve_absent_script_exits_1(self, tmp_path: Path) -> None:
        approvals, _, _, _ = _make_paths(tmp_path)
        _write_approvals(approvals, {})
        with pytest.raises(SystemExit) as exc_info:
            _run(["approvals", "approve", "nonexistent"], tmp_path)
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# approvals reject
# ---------------------------------------------------------------------------


class TestApprovalsReject:
    def test_reject_pending_script(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,  # type: ignore[type-arg]
    ) -> None:
        approvals, _, pending, _ = _make_paths(tmp_path)
        _write_approvals(approvals, {"s": {"state": "pending", "pending_md5": "abc"}})
        (pending / "s").write_text("bad script", encoding="utf-8")
        _run(["approvals", "reject", "s"], tmp_path)
        assert "rejected" in capsys.readouterr().out.lower()
        data = json.loads(approvals.read_text())
        assert data["scripts"]["s"]["state"] == "rejected"

    def test_reject_wrong_state_exits_1(self, tmp_path: Path) -> None:
        approvals, _, _, _ = _make_paths(tmp_path)
        _write_approvals(approvals, {"s": {"state": "approved", "approved_md5": "abc"}})
        with pytest.raises(SystemExit) as exc_info:
            _run(["approvals", "reject", "s"], tmp_path)
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# approvals clear
# ---------------------------------------------------------------------------


class TestApprovalsClear:
    def test_clear_approved_script(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,  # type: ignore[type-arg]
    ) -> None:
        approvals, scripts, _, _ = _make_paths(tmp_path)
        _write_approvals(approvals, {"s": {"state": "approved", "approved_md5": "abc"}})
        (scripts / "s").write_text("content", encoding="utf-8")
        _run(["approvals", "clear", "s"], tmp_path)
        assert "absent" in capsys.readouterr().out
        data = json.loads(approvals.read_text())
        assert "s" not in data["scripts"]

    def test_clear_rejected_script(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,  # type: ignore[type-arg]
    ) -> None:
        approvals, _, _, _ = _make_paths(tmp_path)
        _write_approvals(approvals, {"s": {"state": "rejected"}})
        _run(["approvals", "clear", "s"], tmp_path)
        data = json.loads(approvals.read_text())
        assert "s" not in data["scripts"]

    def test_clear_absent_script_exits_1(self, tmp_path: Path) -> None:
        approvals, _, _, _ = _make_paths(tmp_path)
        _write_approvals(approvals, {})
        with pytest.raises(SystemExit) as exc_info:
            _run(["approvals", "clear", "ghost"], tmp_path)
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# policy show
# ---------------------------------------------------------------------------


class TestPolicyShow:
    def test_empty_list_message(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,  # type: ignore[type-arg]
    ) -> None:
        _make_paths(tmp_path)
        _run(["policy", "show"], tmp_path, auto_approve=[])
        assert "empty" in capsys.readouterr().out.lower()

    def test_shows_listed_scripts(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,  # type: ignore[type-arg]
    ) -> None:
        _make_paths(tmp_path)
        _run(["policy", "show"], tmp_path, auto_approve=["safe_script"])
        assert "safe_script" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# policy auto-approve / manual
# ---------------------------------------------------------------------------


class TestPolicyAutoApprove:
    def test_adds_script_to_config(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,  # type: ignore[type-arg]
    ) -> None:
        _make_paths(tmp_path)
        _run(["policy", "auto-approve", "safe_script"], tmp_path)
        capsys.readouterr()
        config_path = tmp_path / ".csl" / "config.yaml"
        data = yaml.safe_load(config_path.read_text())
        assert "safe_script" in data["approval_policy"]["auto_approve"]

    def test_idempotent_add(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,  # type: ignore[type-arg]
    ) -> None:
        _make_paths(tmp_path)
        _run(["policy", "auto-approve", "s"], tmp_path, auto_approve=["s"])
        out = capsys.readouterr().out
        assert "already" in out

    def test_manual_removes_from_config(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,  # type: ignore[type-arg]
    ) -> None:
        _make_paths(tmp_path)
        _run(["policy", "manual", "safe_script"], tmp_path, auto_approve=["safe_script"])
        capsys.readouterr()
        config_path = tmp_path / ".csl" / "config.yaml"
        data = yaml.safe_load(config_path.read_text())
        assert "safe_script" not in data["approval_policy"]["auto_approve"]

    def test_manual_not_in_list_message(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,  # type: ignore[type-arg]
    ) -> None:
        _make_paths(tmp_path)
        _run(["policy", "manual", "ghost"], tmp_path, auto_approve=[])
        assert "not on" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# help / missing subcommand
# ---------------------------------------------------------------------------


class TestHelpPaths:
    def test_approvals_no_subcommand_exits_1(self, tmp_path: Path) -> None:
        _make_paths(tmp_path)
        with pytest.raises(SystemExit) as exc_info:
            _run(["approvals"], tmp_path)
        assert exc_info.value.code == 1

    def test_policy_no_subcommand_exits_1(self, tmp_path: Path) -> None:
        _make_paths(tmp_path)
        with pytest.raises(SystemExit) as exc_info:
            _run(["policy"], tmp_path)
        assert exc_info.value.code == 1
