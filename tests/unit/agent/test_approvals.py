import json
import logging
from pathlib import Path

import pytest

from control_station_lite.agent.approvals import ApprovalError, ApprovalsManager
from control_station_lite.shared.models import ApprovalState

# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def dirs(tmp_path: Path) -> dict[str, Path]:
    return {
        "approvals": tmp_path / "agent" / "approvals.json",
        "scripts": tmp_path / "scripts",
        "pending": tmp_path / "scripts.pending",
    }


@pytest.fixture
def mgr(dirs: dict[str, Path]) -> ApprovalsManager:
    return ApprovalsManager(
        approvals_path=dirs["approvals"],
        scripts_dir=dirs["scripts"],
        pending_dir=dirs["pending"],
    )


@pytest.fixture
def auto_mgr(dirs: dict[str, Path]) -> ApprovalsManager:
    return ApprovalsManager(
        approvals_path=dirs["approvals"],
        scripts_dir=dirs["scripts"],
        pending_dir=dirs["pending"],
        auto_approve_list=["sleep_machine"],
    )


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------


class TestInitialState:
    def test_unknown_script_is_absent(self, mgr: ApprovalsManager) -> None:
        assert mgr.get_state("no_such_script").state == ApprovalState.absent

    def test_list_all_empty_initially(self, mgr: ApprovalsManager) -> None:
        assert mgr.list_all() == []

    def test_missing_approvals_json_is_fine(self, dirs: dict[str, Path]) -> None:
        assert not dirs["approvals"].exists()
        m = ApprovalsManager(
            approvals_path=dirs["approvals"],
            scripts_dir=dirs["scripts"],
            pending_dir=dirs["pending"],
        )
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

    def test_file_written_to_pending_dir(
        self, mgr: ApprovalsManager, dirs: dict[str, Path]
    ) -> None:
        mgr.stage("sleep_machine", "content", "md5")
        assert (dirs["pending"] / "sleep_machine").exists()

    def test_meta_yaml_written_when_provided(
        self, mgr: ApprovalsManager, dirs: dict[str, Path]
    ) -> None:
        mgr.stage("sleep_machine", "content", "md5", meta_yaml="description: test\n")
        assert (dirs["pending"] / "sleep_machine.meta.yaml").exists()

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
        self, auto_mgr: ApprovalsManager, dirs: dict[str, Path]
    ) -> None:
        auto_mgr.stage("sleep_machine", "content", "md5")
        assert (dirs["scripts"] / "sleep_machine").exists()
        assert not (dirs["pending"] / "sleep_machine").exists()

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
    def test_state_becomes_approved(self, mgr: ApprovalsManager, dirs: dict[str, Path]) -> None:
        mgr.stage("s", "content", "md5")
        mgr.approve("s")
        assert mgr.get_state("s").state == ApprovalState.approved

    def test_approved_md5_matches_pending_md5(
        self, mgr: ApprovalsManager, dirs: dict[str, Path]
    ) -> None:
        mgr.stage("s", "content", "deadbeef")
        mgr.approve("s")
        assert mgr.get_state("s").approved_md5 == "deadbeef"

    def test_file_moved_to_scripts_dir(self, mgr: ApprovalsManager, dirs: dict[str, Path]) -> None:
        mgr.stage("s", "content", "md5")
        mgr.approve("s")
        assert (dirs["scripts"] / "s").exists()
        assert not (dirs["pending"] / "s").exists()

    def test_meta_yaml_moved_when_present(
        self, mgr: ApprovalsManager, dirs: dict[str, Path]
    ) -> None:
        mgr.stage("s", "content", "md5", meta_yaml="description: x\n")
        mgr.approve("s")
        assert (dirs["scripts"] / "s.meta.yaml").exists()
        assert not (dirs["pending"] / "s.meta.yaml").exists()

    def test_cannot_approve_absent(self, mgr: ApprovalsManager) -> None:
        with pytest.raises(ApprovalError, match="not found"):
            mgr.approve("no_such_script")

    def test_cannot_approve_already_approved(
        self, mgr: ApprovalsManager, dirs: dict[str, Path]
    ) -> None:
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

    def test_reject_removes_pending_file(
        self, mgr: ApprovalsManager, dirs: dict[str, Path]
    ) -> None:
        mgr.stage("s", "content", "md5")
        mgr.reject("s")
        assert not (dirs["pending"] / "s").exists()

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

    def test_clear_removes_approved_file(
        self, mgr: ApprovalsManager, dirs: dict[str, Path]
    ) -> None:
        mgr.stage("s", "content", "md5")
        mgr.approve("s")
        mgr.clear("s")
        assert not (dirs["scripts"] / "s").exists()

    def test_clear_allows_restage_after_reject(self, mgr: ApprovalsManager) -> None:
        mgr.stage("s", "content", "md5")
        mgr.reject("s")
        mgr.clear("s")
        # After clear the script is absent — staging it is valid again.
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
    def test_state_survives_reload(self, dirs: dict[str, Path]) -> None:
        m1 = ApprovalsManager(
            approvals_path=dirs["approvals"],
            scripts_dir=dirs["scripts"],
            pending_dir=dirs["pending"],
        )
        m1.stage("s", "content", "md5")
        m1.approve("s")

        m2 = ApprovalsManager(
            approvals_path=dirs["approvals"],
            scripts_dir=dirs["scripts"],
            pending_dir=dirs["pending"],
        )
        assert m2.get_state("s").state == ApprovalState.approved
        assert m2.get_state("s").approved_md5 == "md5"

    def test_approvals_json_is_valid_json(
        self, mgr: ApprovalsManager, dirs: dict[str, Path]
    ) -> None:
        mgr.stage("s", "content", "md5")
        raw = json.loads(dirs["approvals"].read_text())
        assert "scripts" in raw
        assert raw["scripts"]["s"]["state"] == "pending"

    def test_atomic_write_leaves_no_tmp_file(
        self, mgr: ApprovalsManager, dirs: dict[str, Path]
    ) -> None:
        mgr.stage("s", "content", "md5")
        assert not dirs["approvals"].with_suffix(".tmp").exists()

    def test_corrupt_json_starts_fresh(self, dirs: dict[str, Path]) -> None:
        dirs["approvals"].parent.mkdir(parents=True, exist_ok=True)
        dirs["approvals"].write_text("not valid json{{{", encoding="utf-8")
        m = ApprovalsManager(
            approvals_path=dirs["approvals"],
            scripts_dir=dirs["scripts"],
            pending_dir=dirs["pending"],
        )
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
