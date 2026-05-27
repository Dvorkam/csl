"""Unit tests for jobs.py helper functions (called directly, not via HTTP)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from control_station_lite.server.api.jobs import _approval_error_response, _get_job_or_404
from control_station_lite.server.db.models import Job
from control_station_lite.shared.models import ApprovalState

# ---------------------------------------------------------------------------
# _get_job_or_404
# ---------------------------------------------------------------------------


async def test_get_job_or_404_returns_job_when_found() -> None:
    mock_job = MagicMock(spec=Job)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = mock_job
    session = AsyncMock(spec=AsyncSession)
    session.execute.return_value = mock_result

    result = await _get_job_or_404("some-uuid", session)

    assert result is mock_job


async def test_get_job_or_404_raises_404_when_missing() -> None:
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    session = AsyncMock(spec=AsyncSession)
    session.execute.return_value = mock_result

    with pytest.raises(HTTPException) as exc_info:
        await _get_job_or_404("no-such-uuid", session)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Job not found"


# ---------------------------------------------------------------------------
# _approval_error_response
# ---------------------------------------------------------------------------


def test_approval_error_response_pending() -> None:
    exc = _approval_error_response(ApprovalState.pending, "my_script")
    assert exc.status_code == 409
    assert exc.detail["approval_error"] == "pending_approval (new)"
    assert exc.detail["agent_state"] == ApprovalState.pending


def test_approval_error_response_update_pending() -> None:
    exc = _approval_error_response(ApprovalState.update_pending, "my_script")
    assert exc.status_code == 409
    assert exc.detail["approval_error"] == "pending_approval (update)"


def test_approval_error_response_rejected() -> None:
    exc = _approval_error_response(ApprovalState.rejected, "my_script")
    assert exc.status_code == 409
    assert exc.detail["approval_error"] == "rejected"


def test_approval_error_response_unknown_state() -> None:
    exc = _approval_error_response("some_other_state", "my_script")
    assert exc.status_code == 409
    assert exc.detail["approval_error"] == "some_other_state"
    assert exc.detail["agent_state"] == "some_other_state"
