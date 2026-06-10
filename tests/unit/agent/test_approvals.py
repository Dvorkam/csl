import json
import logging
import os
from pathlib import Path

import pytest

from control_station_lite.agent.approvals import ApprovalError, ApprovalsManager
from control_station_lite.agent.paths import CslPaths
from control_station_lite.shared.models import ApprovalState

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def paths(tmp_path: Path) -> CslPaths:
    return CslPaths(
        scripts_dir=tmp_path / "scripts",
        pending_dir=tmp_path / "scripts.pending",
        logs_dir=tmp_path / "logs",
        approvals_path=tmp_path / "agent" / "approvals.json",
        state_path=tmp_path / "agent" / "running.json",
    )


@pytest.fixture
def mgr(paths: CslPaths) -> ApprovalsManager:
    return ApprovalsManager(paths)


@pytest.fixture
def auto_mgr(paths: CslPaths) -> ApprovalsManager:
    return ApprovalsManager(paths, auto_approve_list=["sleep_machine"])


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------


class TestInitialState:
    def test_unknown_script_is_absent(self, mgr: ApprovalsManager) -> None:
        assert mgr.get_state("no_such_script").state == ApprovalState.absent

    def test_list_all_empty_initially(self, mgr: ApprovalsManager) -> None:
        assert mgr.list_all() == []

    def test_missing_approvals_json_is_fine(self, paths: CslPaths) -> None:
        assert not paths.approvals_path.exists()
        m = ApprovalsManager(paths)
        assert m.get_state("x").state == ApprovalState.absent


# ---------------------------------------------------------------------------
# stage — absent → pending
# ---------------------------------------------------------------------------


class TestStageFromAbsent:
    def test_returns_pending(self, mgr: ApprovalsManager) -> None:
        result = mgr.stage("sleep_machine", "#!/bin/bash\nsleep 1", "abc123")
        assert result == ApprovalState.pending

    def test_state_is_pending(self, mgr: ApprovalsManager) -> None:
        mgr.stage("sleep_machine", "#!/bin/bash\nsleep 1", "abc123")
        assert mgr.get_state("sleep_machine").state == ApprovalState.pending

    def test_pending_md5_recorded(self, mgr: ApprovalsManager) -> None:
        mgr.stage("sleep_machine", "content", "deadbeef")
        assert mgr.get_state("sleep_machine").pending_md5 == "deadbeef"

    def test_file_written_to_pending_dir(self, mgr: ApprovalsManager, paths: CslPaths) -> None:
        mgr.stage("sleep_machine", "content", "md5")
        assert (paths.pending_dir / "sleep_machine").exists()

    def test_meta_yaml_written_when_provided(self, mgr: ApprovalsManager, paths: CslPaths) -> None:
        mgr.stage("sleep_machine", "content", "md5", meta_yaml="description: test\n")
        assert (paths.pending_dir / "sleep_machine.meta.yaml").exists()

    def test_appears_in_list_all(self, mgr: ApprovalsManager) -> None:
        mgr.stage("sleep_machine", "content", "md5")
        names = [d.name for d in mgr.list_all()]
        assert "sleep_machine" in names


# ---------------------------------------------------------------------------
# stage — auto-approve
# ---------------------------------------------------------------------------


class TestStageAutoApprove:
    def test_returns_approved(self, auto_mgr: ApprovalsManager) -> None:
        result = auto_mgr.stage("sleep_machine", "content", "md5")
        assert result == ApprovalState.approved

    def test_file_in_scripts_dir_not_pending(
        self, auto_mgr: ApprovalsManager, paths: CslPaths
    ) -> None:
        auto_mgr.stage("sleep_machine", "content", "md5")
        assert (paths.scripts_dir / "sleep_machine").exists()
        assert not (paths.pending_dir / "sleep_machine").exists()

    def test_approved_via_auto(self, auto_mgr: ApprovalsManager) -> None:
        auto_mgr.stage("sleep_machine", "content", "md5")
        d = auto_mgr.get_state("sleep_machine")
        assert d.approved_md5 == "md5"

    def test_non_whitelisted_script_still_pending(self, auto_mgr: ApprovalsManager) -> None:
        result = auto_mgr.stage("restart_machine", "content", "md5")
        assert result == ApprovalState.pending


# ---------------------------------------------------------------------------
# stage — idempotency
# ---------------------------------------------------------------------------


class TestStageIdempotency:
    def test_same_md5_twice_is_noop(self, mgr: ApprovalsManager) -> None:
        mgr.stage("s", "content", "md5")
        result = mgr.stage("s", "content", "md5")
        assert result == ApprovalState.pending

    def test_different_md5_updates_pending(self, mgr: ApprovalsManager) -> None:
        mgr.stage("s", "v1", "md5v1")
        mgr.stage("s", "v2", "md5v2")
        assert mgr.get_state("s").pending_md5 == "md5v2"


# ---------------------------------------------------------------------------
# approve: pending → approved
# ---------------------------------------------------------------------------


class TestApproveFromPending:
    def test_state_becomes_approved(self, mgr: ApprovalsManager) -> None:
        mgr.stage("s", "content", "md5")
        mgr.approve("s")
        assert mgr.get_state("s").state == ApprovalState.approved

    def test_approved_md5_matches_pending_md5(self, mgr: ApprovalsManager) -> None:
        mgr.stage("s", "content", "deadbeef")
        mgr.approve("s")
        assert mgr.get_state("s").approved_md5 == "deadbeef"

    def test_file_moved_to_scripts_dir(self, mgr: ApprovalsManager, paths: CslPaths) -> None:
        mgr.stage("s", "content", "md5")
        mgr.approve("s")
        assert (paths.scripts_dir / "s").exists()
        assert not (paths.pending_dir / "s").exists()

    def test_meta_yaml_moved_when_present(self, mgr: ApprovalsManager, paths: CslPaths) -> None:
        mgr.stage("s", "content", "md5", meta_yaml="description: x\n")
        mgr.approve("s")
        assert (paths.scripts_dir / "s.meta.yaml").exists()
        assert not (paths.pending_dir / "s.meta.yaml").exists()

    def test_cannot_approve_absent(self, mgr: ApprovalsManager) -> None:
        with pytest.raises(ApprovalError, match="not found"):
            mgr.approve("no_such_script")

    def test_cannot_approve_already_approved(self, mgr: ApprovalsManager) -> None:
        mgr.stage("s", "content", "md5")
        mgr.approve("s")
        with pytest.raises(ApprovalError):
            mgr.approve("s")


# ---------------------------------------------------------------------------
# stage approved → update_pending
# ---------------------------------------------------------------------------


class TestStageApprovedScript:
    def _approve(self, mgr: ApprovalsManager, name: str, content: str, md5: str) -> None:
        mgr.stage(name, content, md5)
        mgr.approve(name)

    def test_new_content_gives_update_pending(self, mgr: ApprovalsManager) -> None:
        self._approve(mgr, "s", "v1", "md5v1")
        result = mgr.stage("s", "v2", "md5v2")
        assert result == ApprovalState.update_pending

    def test_same_content_stays_approved(self, mgr: ApprovalsManager) -> None:
        self._approve(mgr, "s", "v1", "md5v1")
        result = mgr.stage("s", "v1", "md5v1")
        assert result == ApprovalState.approved

    def test_old_approved_md5_preserved(self, mgr: ApprovalsManager) -> None:
        self._approve(mgr, "s", "v1", "md5v1")
        mgr.stage("s", "v2", "md5v2")
        d = mgr.get_state("s")
        assert d.approved_md5 == "md5v1"
        assert d.pending_md5 == "md5v2"


# ---------------------------------------------------------------------------
# approve: update_pending → approved
# ---------------------------------------------------------------------------


class TestApproveUpdatePending:
    def test_approves_new_version(self, mgr: ApprovalsManager) -> None:
        mgr.stage("s", "v1", "md5v1")
        mgr.approve("s")
        mgr.stage("s", "v2", "md5v2")
        mgr.approve("s")
        d = mgr.get_state("s")
        assert d.state == ApprovalState.approved
        assert d.approved_md5 == "md5v2"


# ---------------------------------------------------------------------------
# reject: pending / update_pending → rejected
# ---------------------------------------------------------------------------


class TestReject:
    def test_reject_pending_gives_rejected(self, mgr: ApprovalsManager) -> None:
        mgr.stage("s", "content", "md5")
        mgr.reject("s")
        assert mgr.get_state("s").state == ApprovalState.rejected

    def test_reject_removes_pending_file(self, mgr: ApprovalsManager, paths: CslPaths) -> None:
        mgr.stage("s", "content", "md5")
        mgr.reject("s")
        assert not (paths.pending_dir / "s").exists()

    def test_reject_update_pending_gives_rejected(self, mgr: ApprovalsManager) -> None:
        mgr.stage("s", "v1", "md5v1")
        mgr.approve("s")
        mgr.stage("s", "v2", "md5v2")
        mgr.reject("s")
        assert mgr.get_state("s").state == ApprovalState.rejected

    def test_cannot_reject_absent(self, mgr: ApprovalsManager) -> None:
        with pytest.raises(ApprovalError, match="not found"):
            mgr.reject("no_such_script")

    def test_cannot_reject_approved(self, mgr: ApprovalsManager) -> None:
        mgr.stage("s", "content", "md5")
        mgr.approve("s")
        with pytest.raises(ApprovalError):
            mgr.reject("s")

    def test_cannot_stage_rejected_script(self, mgr: ApprovalsManager) -> None:
        mgr.stage("s", "content", "md5")
        mgr.reject("s")
        with pytest.raises(ApprovalError, match="rejected"):
            mgr.stage("s", "new content", "new_md5")


# ---------------------------------------------------------------------------
# clear: any state → absent
# ---------------------------------------------------------------------------


class TestClear:
    def test_clear_pending_gives_absent(self, mgr: ApprovalsManager) -> None:
        mgr.stage("s", "content", "md5")
        mgr.clear("s")
        assert mgr.get_state("s").state == ApprovalState.absent

    def test_clear_approved_gives_absent(self, mgr: ApprovalsManager) -> None:
        mgr.stage("s", "content", "md5")
        mgr.approve("s")
        mgr.clear("s")
        assert mgr.get_state("s").state == ApprovalState.absent

    def test_clear_rejected_gives_absent(self, mgr: ApprovalsManager) -> None:
        mgr.stage("s", "content", "md5")
        mgr.reject("s")
        mgr.clear("s")
        assert mgr.get_state("s").state == ApprovalState.absent

    def test_clear_absent_raises(self, mgr: ApprovalsManager) -> None:
        with pytest.raises(ApprovalError, match="not found"):
            mgr.clear("no_such_script")

    def test_clear_removes_approved_file(self, mgr: ApprovalsManager, paths: CslPaths) -> None:
        mgr.stage("s", "content", "md5")
        mgr.approve("s")
        mgr.clear("s")
        assert not (paths.scripts_dir / "s").exists()

    def test_clear_allows_restage_after_reject(self, mgr: ApprovalsManager) -> None:
        mgr.stage("s", "content", "md5")
        mgr.reject("s")
        mgr.clear("s")
        result = mgr.stage("s", "content", "md5")
        assert result == ApprovalState.pending

    def test_clear_rejected_script_not_in_store(self, mgr: ApprovalsManager) -> None:
        mgr.stage("s", "content", "md5")
        mgr.reject("s")
        mgr.clear("s")
        assert "s" not in mgr._store.scripts  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_state_survives_reload(self, paths: CslPaths) -> None:
        m1 = ApprovalsManager(paths)
        m1.stage("s", "content", "md5")
        m1.approve("s")

        m2 = ApprovalsManager(paths)
        assert m2.get_state("s").state == ApprovalState.approved
        assert m2.get_state("s").approved_md5 == "md5"

    def test_external_approval_visible_to_running_instance(self, paths: CslPaths) -> None:
        """A running server instance must pick up approvals done by a separate CLI process.

        Regression test for the bug where ApprovalsManager cached state in memory
        and never re-read the file, so CLI approvals were invisible to the HTTP agent.
        """
        server_mgr = ApprovalsManager(paths)
        server_mgr.stage("hello", "content", "abc123")
        assert server_mgr.get_state("hello").state == ApprovalState.pending

        # Simulate CLI running as a separate process
        cli_mgr = ApprovalsManager(paths)
        cli_mgr.approve("hello")

        # The server manager must now see the updated state without restarting
        assert server_mgr.get_state("hello").state == ApprovalState.approved

    def test_reload_detected_when_mtime_unchanged(self, paths: CslPaths) -> None:
        """External change within the same mtime tick must still be picked up.

        On filesystems with coarse mtime resolution a stage-then-approve can
        leave the file's mtime unchanged; the size comparison in the change
        signature is what catches it. Here we pin the mtime to a constant so a
        bare-mtime check would miss the update and the store would stay stale.
        """
        server_mgr = ApprovalsManager(paths)
        server_mgr.stage("hello", "content", "abc123")
        assert server_mgr.get_state("hello").state == ApprovalState.pending

        # Capture the file's exact mtime, then approve via a separate manager.
        before = paths.approvals_path.stat()
        ApprovalsManager(paths).approve("hello")

        # Force the mtime back to its pre-approve value: a bare-mtime check now
        # sees "no change", but the file size has grown.
        os.utime(paths.approvals_path, ns=(before.st_atime_ns, before.st_mtime_ns))

        assert server_mgr.get_state("hello").state == ApprovalState.approved

    def test_approvals_json_is_valid_json(self, mgr: ApprovalsManager, paths: CslPaths) -> None:
        mgr.stage("s", "content", "md5")
        raw = json.loads(paths.approvals_path.read_text())
        assert "scripts" in raw
        assert raw["scripts"]["s"]["state"] == "pending"

    def test_atomic_write_leaves_no_tmp_file(self, mgr: ApprovalsManager, paths: CslPaths) -> None:
        mgr.stage("s", "content", "md5")
        assert not paths.approvals_path.with_suffix(".tmp").exists()

    def test_corrupt_json_starts_fresh(self, paths: CslPaths) -> None:
        paths.approvals_path.parent.mkdir(parents=True, exist_ok=True)
        paths.approvals_path.write_text("not valid json{{{", encoding="utf-8")
        m = ApprovalsManager(paths)
        assert m.get_state("s").state == ApprovalState.absent


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


class TestAuditLog:
    def test_stage_emits_audit_entry(
        self, mgr: ApprovalsManager, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger="csl.agent.audit"):
            mgr.stage("s", "content", "md5")
        assert any("action=stage" in r.message and "script=s" in r.message for r in caplog.records)

    def test_approve_emits_audit_entry(
        self, mgr: ApprovalsManager, caplog: pytest.LogCaptureFixture
    ) -> None:
        mgr.stage("s", "content", "md5")
        with caplog.at_level(logging.INFO, logger="csl.agent.audit"):
            mgr.approve("s")
        assert any("action=approve" in r.message for r in caplog.records)

    def test_reject_emits_audit_entry(
        self, mgr: ApprovalsManager, caplog: pytest.LogCaptureFixture
    ) -> None:
        mgr.stage("s", "content", "md5")
        with caplog.at_level(logging.INFO, logger="csl.agent.audit"):
            mgr.reject("s")
        assert any("action=reject" in r.message for r in caplog.records)

    def test_clear_emits_audit_entry(
        self, mgr: ApprovalsManager, caplog: pytest.LogCaptureFixture
    ) -> None:
        mgr.stage("s", "content", "md5")
        with caplog.at_level(logging.INFO, logger="csl.agent.audit"):
            mgr.clear("s")
        assert any("action=clear" in r.message for r in caplog.records)
