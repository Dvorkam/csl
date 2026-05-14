from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from control_station_lite.shared.models import (
    AgentHealth,
    ApprovalState,
    JobRequest,
    JobStatus,
    JobStatusResponse,
    LogChunk,
    ScriptDescriptor,
    StageScriptRequest,
    StageScriptResponse,
)


class TestApprovalState:
    def test_all_values_accessible(self) -> None:
        assert ApprovalState.absent == "absent"
        assert ApprovalState.pending == "pending"
        assert ApprovalState.approved == "approved"
        assert ApprovalState.update_pending == "update_pending"
        assert ApprovalState.rejected == "rejected"

    def test_is_string_enum(self) -> None:
        assert isinstance(ApprovalState.approved, str)

    def test_roundtrip_via_value(self) -> None:
        assert ApprovalState("update_pending") is ApprovalState.update_pending


class TestJobStatus:
    def test_all_statuses(self) -> None:
        for val in ("pending", "running", "completed", "failed", "killed"):
            assert JobStatus(val).value == val


class TestJobRequest:
    def test_minimal(self) -> None:
        req = JobRequest(job_uuid="abc-123", script_name="sleep_machine")
        assert req.params == {}
        assert req.persistent is False

    def test_with_params(self) -> None:
        req = JobRequest(
            job_uuid="abc-123",
            script_name="start_llama",
            params={"model_path": "/models/llama.gguf", "context_size": 4096, "verbose": True},
            persistent=True,
        )
        assert req.params["context_size"] == 4096
        assert req.persistent is True

    def test_missing_required_fields(self) -> None:
        with pytest.raises(ValidationError):
            JobRequest(script_name="sleep_machine")  # type: ignore[call-arg]


class TestJobStatusResponse:
    def test_minimal(self) -> None:
        r = JobStatusResponse(
            job_uuid="abc",
            script_name="foo",
            status=JobStatus.running,
            persistent=False,
            started_at=datetime(2026, 5, 14, 10, 0, tzinfo=UTC),
        )
        assert r.ended_at is None
        assert r.exit_code is None

    def test_completed(self) -> None:
        now = datetime(2026, 5, 14, 10, 0, tzinfo=UTC)
        r = JobStatusResponse(
            job_uuid="abc",
            script_name="foo",
            status=JobStatus.completed,
            persistent=False,
            started_at=now,
            ended_at=now,
            exit_code=0,
        )
        assert r.exit_code == 0


class TestLogChunk:
    def test_stdout(self) -> None:
        chunk = LogChunk(
            job_uuid="abc",
            line="hello",
            stream="stdout",
            timestamp=datetime(2026, 5, 14, tzinfo=UTC),
        )
        assert chunk.stream == "stdout"

    def test_invalid_stream(self) -> None:
        with pytest.raises(ValidationError):
            LogChunk(
                job_uuid="abc",
                line="hello",
                stream="stdin",  # type: ignore[arg-type]
                timestamp=datetime(2026, 5, 14, tzinfo=UTC),
            )


class TestAgentHealth:
    def test_basic(self) -> None:
        h = AgentHealth(version="0.1.0", running_persistent_jobs=2, idle_seconds=0.0)
        assert h.running_persistent_jobs == 2


class TestScriptDescriptor:
    def test_defaults(self) -> None:
        d = ScriptDescriptor(name="sleep_machine", state=ApprovalState.absent)
        assert d.persistent is False
        assert d.approved_md5 is None
        assert d.pending_md5 is None

    def test_approved_with_md5(self) -> None:
        d = ScriptDescriptor(
            name="sleep_machine",
            state=ApprovalState.approved,
            approved_md5="a1b2c3",
        )
        assert d.approved_md5 == "a1b2c3"


class TestStageScriptRequest:
    def test_without_meta(self) -> None:
        r = StageScriptRequest(content="#!/bin/bash\necho hi", md5="deadbeef")
        assert r.meta_yaml is None

    def test_with_meta(self) -> None:
        r = StageScriptRequest(
            content="#!/bin/bash\necho hi",
            md5="deadbeef",
            meta_yaml="description: test\npersistent: false\n",
        )
        assert r.meta_yaml is not None


class TestStageScriptResponse:
    def test_pending(self) -> None:
        r = StageScriptResponse(name="sleep_machine", state=ApprovalState.pending)
        assert r.state == ApprovalState.pending

    def test_auto_approved(self) -> None:
        r = StageScriptResponse(name="sleep_machine", state=ApprovalState.approved)
        assert r.state is ApprovalState.approved
