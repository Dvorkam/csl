"""Tests for the `csl-admin seed-scripts` CLI command."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_station_lite.server import cli
from control_station_lite.server.auth.password import hash_password
from control_station_lite.server.db.models import Base, Script, User


@pytest.fixture
async def factory(monkeypatch: pytest.MonkeyPatch):
    """In-memory DB wired in place of the real session factory."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    # _seed_scripts imports _session_factory from this module at call time.
    monkeypatch.setattr(
        "control_station_lite.server.db.session._session_factory",
        lambda: sessionmaker,
    )
    yield sessionmaker
    await engine.dispose()


async def _add_admin(factory: async_sessionmaker) -> None:
    async with factory() as s:
        s.add(User(username="admin", password_hash=hash_password("x" * 8), role="admin"))
        await s.commit()


class TestSeedScriptsCommand:
    async def test_seeds_catalogue_when_admin_exists(self, factory, capsys) -> None:
        await _add_admin(factory)

        await cli._seed_scripts()

        async with factory() as s:
            rows = (await s.execute(select(Script))).scalars().all()
        assert len(rows) > 0
        # Attributed to the admin user.
        assert all(r.updated_by == 1 for r in rows)
        assert "added" in capsys.readouterr().out

    async def test_exits_when_no_admin(self, factory) -> None:
        with pytest.raises(SystemExit) as exc:
            await cli._seed_scripts()
        assert exc.value.code == 1

    async def test_is_idempotent_across_runs(self, factory, capsys) -> None:
        await _add_admin(factory)
        await cli._seed_scripts()
        async with factory() as s:
            first = len((await s.execute(select(Script))).scalars().all())

        await cli._seed_scripts()
        async with factory() as s:
            second = len((await s.execute(select(Script))).scalars().all())
        assert first == second
