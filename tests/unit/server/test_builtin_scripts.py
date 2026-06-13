"""Unit tests for server/core/builtin_scripts.py and the packaged catalogue."""

import hashlib

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from control_station_lite.server.core.builtin_scripts import (
    BUILTIN_SCRIPTS_DIR,
    iter_builtin_scripts,
    seed_builtin_scripts,
)
from control_station_lite.server.db.models import Base, Script
from control_station_lite.shared.script_meta import parse_meta_yaml

# Names the shipped catalogue must contain (Phase 11; OS-natural split).
_EXPECTED_BUILTINS = {
    "sleep_machine.sh",
    "sleep_machine.ps1",
    "restart_machine.sh",
    "restart_machine.ps1",
    "start_steam.ps1",
    "start_llama_server.ps1",
}


@pytest.fixture
async def session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


# ---------------------------------------------------------------------------
# Packaged catalogue
# ---------------------------------------------------------------------------


class TestPackagedCatalogue:
    def test_discovers_expected_scripts(self) -> None:
        names = {b.name for b in iter_builtin_scripts()}
        assert names == _EXPECTED_BUILTINS

    def test_every_script_has_spdx_header(self) -> None:
        for builtin in iter_builtin_scripts():
            first = builtin.content.splitlines()[0]
            assert "SPDX-License-Identifier: AGPL-3.0-or-later" in first, builtin.name

    def test_every_meta_parses(self) -> None:
        for builtin in iter_builtin_scripts():
            assert builtin.meta_yaml is not None, builtin.name
            parse_meta_yaml(builtin.meta_yaml)  # raises on invalid

    def test_cross_platform_pair_shares_metadata(self) -> None:
        by_name = {b.name: b for b in iter_builtin_scripts()}
        assert by_name["sleep_machine.sh"].meta_yaml == by_name["sleep_machine.ps1"].meta_yaml

    def test_llama_server_is_persistent_with_params(self) -> None:
        by_name = {b.name: b for b in iter_builtin_scripts()}
        meta = parse_meta_yaml(by_name["start_llama_server.ps1"].meta_yaml or "")
        assert meta.persistent is True
        assert {p.name for p in meta.params} == {"model_path", "context_size", "gpu_layers"}

    def test_dir_is_under_the_package(self) -> None:
        assert BUILTIN_SCRIPTS_DIR.is_dir()
        assert BUILTIN_SCRIPTS_DIR.name == "builtin_scripts"


# ---------------------------------------------------------------------------
# iter_builtin_scripts (custom directory)
# ---------------------------------------------------------------------------


class TestIterCustomDir:
    def test_pairs_script_with_shared_meta(self, tmp_path) -> None:
        (tmp_path / "foo.sh").write_text("echo foo\n")
        (tmp_path / "foo.meta.yaml").write_text("description: foo\n")
        (tmp_path / "foo.ps1").write_text("Write-Output foo\n")

        builtins = {b.name: b for b in iter_builtin_scripts(tmp_path)}
        assert set(builtins) == {"foo.sh", "foo.ps1"}
        assert builtins["foo.sh"].meta_yaml == "description: foo\n"
        assert builtins["foo.ps1"].meta_yaml == "description: foo\n"

    def test_script_without_meta_yields_none(self, tmp_path) -> None:
        (tmp_path / "bare.sh").write_text("echo bare\n")
        (builtin,) = iter_builtin_scripts(tmp_path)
        assert builtin.name == "bare.sh"
        assert builtin.meta_yaml is None

    def test_ignores_non_script_files(self, tmp_path) -> None:
        (tmp_path / "readme.txt").write_text("not a script\n")
        (tmp_path / "x.meta.yaml").write_text("description: x\n")
        assert iter_builtin_scripts(tmp_path) == []


# ---------------------------------------------------------------------------
# seed_builtin_scripts
# ---------------------------------------------------------------------------


class TestSeed:
    async def test_seeds_full_catalogue(self, session: AsyncSession) -> None:
        result = await seed_builtin_scripts(session, user_id=1)
        await session.commit()
        assert set(result.created) == _EXPECTED_BUILTINS
        assert result.skipped == []

        rows = (await session.execute(select(Script))).scalars().all()
        assert {r.name for r in rows} == _EXPECTED_BUILTINS

    async def test_rows_have_correct_md5_and_persistence(self, session: AsyncSession) -> None:
        await seed_builtin_scripts(session, user_id=1)
        await session.commit()
        rows = {r.name: r for r in (await session.execute(select(Script))).scalars().all()}

        llama = rows["start_llama_server.ps1"]
        assert llama.persistent is True
        assert llama.md5 == hashlib.md5(llama.content.encode()).hexdigest()
        assert rows["sleep_machine.sh"].persistent is False

    async def test_second_run_is_idempotent(self, session: AsyncSession) -> None:
        await seed_builtin_scripts(session, user_id=1)
        await session.commit()
        result = await seed_builtin_scripts(session, user_id=1)
        await session.commit()
        assert result.created == []
        assert set(result.skipped) == _EXPECTED_BUILTINS

    async def test_does_not_overwrite_admin_edits(self, session: AsyncSession) -> None:
        await seed_builtin_scripts(session, user_id=1)
        await session.commit()
        edited = (
            await session.execute(select(Script).where(Script.name == "sleep_machine.sh"))
        ).scalar_one()
        edited.content = "echo edited by admin\n"
        edited.md5 = hashlib.md5(edited.content.encode()).hexdigest()
        await session.commit()

        result = await seed_builtin_scripts(session, user_id=1)
        await session.commit()
        assert "sleep_machine.sh" in result.skipped

        again = (
            await session.execute(select(Script).where(Script.name == "sleep_machine.sh"))
        ).scalar_one()
        assert again.content == "echo edited by admin\n"

    async def test_only_missing_scripts_are_added(self, session: AsyncSession, tmp_path) -> None:
        (tmp_path / "a.sh").write_text("echo a\n")
        (tmp_path / "b.ps1").write_text("Write-Output b\n")

        first = await seed_builtin_scripts(session, user_id=1, directory=tmp_path)
        await session.commit()
        assert set(first.created) == {"a.sh", "b.ps1"}

        (tmp_path / "c.sh").write_text("echo c\n")
        second = await seed_builtin_scripts(session, user_id=1, directory=tmp_path)
        await session.commit()
        assert second.created == ["c.sh"]
        assert set(second.skipped) == {"a.sh", "b.ps1"}
