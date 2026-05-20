"""Unit tests for server/db/session.py."""

from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from control_station_lite.server.db.session import _session_factory, get_session


@pytest.fixture(autouse=True)
def clear_session_cache() -> None:
    """Ensure each test starts with a clean session factory cache."""
    _session_factory.cache_clear()
    yield  # type: ignore[misc]
    _session_factory.cache_clear()


def _mock_settings(db_url: str = "sqlite+aiosqlite:///:memory:") -> MagicMock:
    m = MagicMock()
    m.database_url = db_url
    return m


# ---------------------------------------------------------------------------
# _session_factory
# ---------------------------------------------------------------------------


def test_session_factory_returns_async_sessionmaker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "control_station_lite.server.db.session.get_settings",
        lambda: _mock_settings(),
    )
    factory = _session_factory()
    assert isinstance(factory, async_sessionmaker)


def test_session_factory_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    call_count = 0

    def counting_settings() -> MagicMock:
        nonlocal call_count
        call_count += 1
        return _mock_settings()

    monkeypatch.setattr(
        "control_station_lite.server.db.session.get_settings",
        counting_settings,
    )
    _session_factory()
    _session_factory()
    assert call_count == 1  # settings read only once


# ---------------------------------------------------------------------------
# get_session
# ---------------------------------------------------------------------------


async def test_get_session_yields_async_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "control_station_lite.server.db.session.get_settings",
        lambda: _mock_settings(),
    )
    async for session in get_session():
        assert isinstance(session, AsyncSession)
