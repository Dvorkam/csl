"""Unit tests for the audit-log helper (Task 9.2)."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from control_station_lite.server.core.audit import record_audit
from control_station_lite.server.db.models import AuditLog, Base


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


async def _all(session) -> list[AuditLog]:
    return list((await session.execute(select(AuditLog))).scalars().all())


async def test_record_audit_flush_only_joins_callers_transaction(session) -> None:
    await record_audit(session, action="x.test", target_type="thing", target_id=7, user_id=1)
    # Flushed but not committed: visible in this session, rolls back on rollback.
    assert len(await _all(session)) == 1
    await session.rollback()
    assert await _all(session) == []


async def test_record_audit_commit_persists(session) -> None:
    await record_audit(session, action="x.test", target_type="thing", target_id=7, commit=True)
    await session.rollback()
    rows = await _all(session)
    assert len(rows) == 1
    assert rows[0].action == "x.test"


async def test_record_audit_serialises_details_and_stringifies_target(session) -> None:
    await record_audit(
        session,
        action="x.test",
        target_type="thing",
        target_id=42,
        result="failure",
        user_id=3,
        details={"k": "v", "n": 1},
    )
    row = (await _all(session))[0]
    assert row.target_id == "42"
    assert row.result == "failure"
    assert row.user_id == 3
    assert row.details_json == '{"k": "v", "n": 1}'


async def test_record_audit_none_target_becomes_empty_string(session) -> None:
    await record_audit(session, action="x.test", target_type="thing", target_id=None)
    row = (await _all(session))[0]
    assert row.target_id == ""
    assert row.details_json is None
