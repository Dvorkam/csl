"""Unit tests for server/core/script_registry.py."""

import hashlib

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from control_station_lite.server.core.script_registry import (
    ScriptRegistryError,
    _compute_md5,
    create_script,
    delete_script,
    get_script,
    get_script_or_raise,
    list_scripts,
    update_script,
)
from control_station_lite.server.db.models import Base


@pytest.fixture
async def session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


_CONTENT = "#!/bin/bash\necho hello\n"
_META = "description: says hello\npersistent: false\n"
_META_PERSISTENT = "description: long-running\npersistent: true\n"


# ---------------------------------------------------------------------------
# _compute_md5
# ---------------------------------------------------------------------------


def test_compute_md5_matches_hashlib() -> None:
    assert _compute_md5(_CONTENT) == hashlib.md5(_CONTENT.encode()).hexdigest()


def test_compute_md5_changes_on_content_change() -> None:
    assert _compute_md5("a") != _compute_md5("b")


# ---------------------------------------------------------------------------
# create_script
# ---------------------------------------------------------------------------


class TestCreateScript:
    async def test_creates_row(self, session: AsyncSession) -> None:
        script = await create_script(
            name="hello", content=_CONTENT, meta_yaml=_META, user_id=1, session=session
        )
        await session.commit()
        assert script.id is not None
        assert script.name == "hello"
        assert script.md5 == _compute_md5(_CONTENT)

    async def test_md5_auto_computed(self, session: AsyncSession) -> None:
        script = await create_script(
            name="s", content=_CONTENT, meta_yaml=None, user_id=1, session=session
        )
        await session.commit()
        assert script.md5 == hashlib.md5(_CONTENT.encode()).hexdigest()

    async def test_persistent_flag_from_meta(self, session: AsyncSession) -> None:
        s = await create_script(
            name="p", content="x", meta_yaml=_META_PERSISTENT, user_id=1, session=session
        )
        await session.commit()
        assert s.persistent is True

    async def test_no_meta_persistent_is_false(self, session: AsyncSession) -> None:
        s = await create_script(name="np", content="x", meta_yaml=None, user_id=1, session=session)
        await session.commit()
        assert s.persistent is False

    async def test_duplicate_name_raises(self, session: AsyncSession) -> None:
        await create_script(name="dup", content="a", meta_yaml=None, user_id=1, session=session)
        await session.commit()
        with pytest.raises(ScriptRegistryError, match="already exists"):
            await create_script(name="dup", content="b", meta_yaml=None, user_id=1, session=session)

    async def test_invalid_meta_raises(self, session: AsyncSession) -> None:
        with pytest.raises(ScriptRegistryError, match="invalid meta YAML"):
            await create_script(
                name="bad", content="x", meta_yaml=": bad: yaml: {{", user_id=1, session=session
            )


# ---------------------------------------------------------------------------
# get_script / get_script_or_raise
# ---------------------------------------------------------------------------


class TestGetScript:
    async def test_returns_none_when_missing(self, session: AsyncSession) -> None:
        assert await get_script("nope", session) is None

    async def test_returns_script_when_exists(self, session: AsyncSession) -> None:
        await create_script(name="s", content="c", meta_yaml=None, user_id=1, session=session)
        await session.commit()
        result = await get_script("s", session)
        assert result is not None
        assert result.name == "s"

    async def test_or_raise_raises_on_missing(self, session: AsyncSession) -> None:
        with pytest.raises(ScriptRegistryError, match="not found"):
            await get_script_or_raise("ghost", session)


# ---------------------------------------------------------------------------
# list_scripts
# ---------------------------------------------------------------------------


class TestListScripts:
    async def test_empty_returns_empty_list(self, session: AsyncSession) -> None:
        assert await list_scripts(session) == []

    async def test_returns_all_scripts_ordered(self, session: AsyncSession) -> None:
        await create_script(name="zzz", content="z", meta_yaml=None, user_id=1, session=session)
        await create_script(name="aaa", content="a", meta_yaml=None, user_id=1, session=session)
        await session.commit()
        names = [s.name for s in await list_scripts(session)]
        assert names == ["aaa", "zzz"]


# ---------------------------------------------------------------------------
# update_script
# ---------------------------------------------------------------------------


class TestUpdateScript:
    async def test_updates_content_and_md5(self, session: AsyncSession) -> None:
        await create_script(name="s", content="old", meta_yaml=None, user_id=1, session=session)
        await session.commit()
        s = await update_script(name="s", content="new", meta_yaml=None, user_id=1, session=session)
        await session.commit()
        assert s.content == "new"
        assert s.md5 == _compute_md5("new")

    async def test_updates_meta_and_persistent_flag(self, session: AsyncSession) -> None:
        await create_script(name="s", content="x", meta_yaml=None, user_id=1, session=session)
        await session.commit()
        s = await update_script(
            name="s", content="x", meta_yaml=_META_PERSISTENT, user_id=1, session=session
        )
        await session.commit()
        assert s.persistent is True

    async def test_missing_script_raises(self, session: AsyncSession) -> None:
        with pytest.raises(ScriptRegistryError, match="not found"):
            await update_script(
                name="ghost", content="x", meta_yaml=None, user_id=1, session=session
            )

    async def test_invalid_meta_raises(self, session: AsyncSession) -> None:
        await create_script(name="s", content="x", meta_yaml=None, user_id=1, session=session)
        await session.commit()
        with pytest.raises(ScriptRegistryError, match="invalid meta YAML"):
            await update_script(
                name="s", content="x", meta_yaml="bad: {{{", user_id=1, session=session
            )


# ---------------------------------------------------------------------------
# delete_script
# ---------------------------------------------------------------------------


class TestDeleteScript:
    async def test_deletes_script(self, session: AsyncSession) -> None:
        await create_script(name="s", content="x", meta_yaml=None, user_id=1, session=session)
        await session.commit()
        await delete_script("s", session)
        await session.commit()
        assert await get_script("s", session) is None

    async def test_missing_raises(self, session: AsyncSession) -> None:
        with pytest.raises(ScriptRegistryError, match="not found"):
            await delete_script("ghost", session)

    async def test_delete_cascades_target_state(self, session: AsyncSession) -> None:
        from control_station_lite.server.db.models import ScriptTargetState

        s = await create_script(name="s", content="x", meta_yaml=None, user_id=1, session=session)
        await session.commit()
        await session.refresh(s)
        from datetime import datetime

        session.add(
            ScriptTargetState(
                machine_id=1,
                script_id=s.id,
                state="approved",
                approved_md5=s.md5,
                pending_md5=None,
                last_refreshed_at=datetime.utcnow(),
            )
        )
        await session.commit()
        await delete_script("s", session)
        await session.commit()
        from sqlalchemy import select

        rows = await session.execute(
            select(ScriptTargetState).where(ScriptTargetState.script_id == s.id)
        )
        assert rows.scalar_one_or_none() is None
