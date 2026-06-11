"""Unit tests for server/db/models.py."""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from control_station_lite.server.db.models import (
    AuditLog,
    Base,
    Job,
    Machine,
    RefreshToken,
    Script,
    ScriptTargetState,
    User,
    UserMachine,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_MEMORY_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:  # type: ignore[name-defined]
    engine = create_async_engine(_MEMORY_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


# ---------------------------------------------------------------------------
# Structural: all expected tables present
# ---------------------------------------------------------------------------

EXPECTED_TABLES = {
    "users",
    "refresh_tokens",
    "machines",
    "user_machines",
    "scripts",
    "script_target_state",
    "jobs",
    "audit_log",
}


def test_all_table_names_defined() -> None:
    assert set(Base.metadata.tables.keys()) == EXPECTED_TABLES


# ---------------------------------------------------------------------------
# Structural: spot-check columns and nullability via metadata
# ---------------------------------------------------------------------------


def _col(table_name: str, col_name: str):  # type: ignore[return]
    return Base.metadata.tables[table_name].c[col_name]


def test_users_required_columns() -> None:
    for col in ("id", "username", "password_hash", "role", "created_at", "disabled"):
        assert col in Base.metadata.tables["users"].c


def test_machines_mac_address_is_nullable() -> None:
    assert _col("machines", "mac_address").nullable is True


def test_machines_ssh_host_is_not_nullable() -> None:
    assert _col("machines", "ssh_host").nullable is False


def test_machines_ssh_host_key_is_nullable() -> None:
    assert _col("machines", "ssh_host_key").nullable is True


def test_jobs_script_id_is_nullable() -> None:
    assert _col("jobs", "script_id").nullable is True


def test_jobs_ended_at_is_nullable() -> None:
    assert _col("jobs", "ended_at").nullable is True


def test_jobs_exit_code_is_nullable() -> None:
    assert _col("jobs", "exit_code").nullable is True


def test_scripts_meta_yaml_is_nullable() -> None:
    assert _col("scripts", "meta_yaml").nullable is True


def test_script_target_state_approved_md5_is_nullable() -> None:
    assert _col("script_target_state", "approved_md5").nullable is True


def test_audit_log_user_id_is_nullable() -> None:
    assert _col("audit_log", "user_id").nullable is True


def test_audit_log_details_json_is_nullable() -> None:
    assert _col("audit_log", "details_json").nullable is True


def test_user_machine_composite_pk() -> None:
    pk_cols = {c.name for c in Base.metadata.tables["user_machines"].primary_key}
    assert pk_cols == {"user_id", "machine_id"}


def test_script_target_state_composite_pk() -> None:
    pk_cols = {c.name for c in Base.metadata.tables["script_target_state"].primary_key}
    assert pk_cols == {"machine_id", "script_id"}


# ---------------------------------------------------------------------------
# Behavioural: schema creates on SQLite without error
# ---------------------------------------------------------------------------


async def test_schema_creates_without_error() -> None:
    engine = create_async_engine(_MEMORY_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        tables = await conn.run_sync(
            lambda c: inspect(c).get_table_names()  # type: ignore[arg-type]
        )
    await engine.dispose()
    assert EXPECTED_TABLES.issubset(set(tables))


# ---------------------------------------------------------------------------
# Behavioural: User round-trip and unique constraint
# ---------------------------------------------------------------------------


async def test_user_insert_and_query(db_session: AsyncSession) -> None:
    user = User(username="alice", password_hash="bcrypt_hash", role="user")
    db_session.add(user)
    await db_session.commit()

    result = await db_session.execute(select(User).where(User.username == "alice"))
    fetched = result.scalar_one()
    assert fetched.username == "alice"
    assert fetched.role == "user"
    assert fetched.disabled is False
    assert isinstance(fetched.created_at, datetime)


async def test_user_username_unique_constraint(db_session: AsyncSession) -> None:
    db_session.add(User(username="bob", password_hash="h1", role="user"))
    await db_session.commit()

    db_session.add(User(username="bob", password_hash="h2", role="admin"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


# ---------------------------------------------------------------------------
# Behavioural: RefreshToken
# ---------------------------------------------------------------------------


async def test_refresh_token_insert(db_session: AsyncSession) -> None:
    user = User(username="carol", password_hash="h", role="user")
    db_session.add(user)
    await db_session.commit()

    now = datetime.now(UTC).replace(tzinfo=None)
    token = RefreshToken(
        user_id=user.id,
        token_hash="sha256_hash",
        issued_at=now,
        expires_at=now,
    )
    db_session.add(token)
    await db_session.commit()

    result = await db_session.execute(
        select(RefreshToken).where(RefreshToken.token_hash == "sha256_hash")
    )
    fetched = result.scalar_one()
    assert fetched.user_id == user.id
    assert fetched.revoked is False


# ---------------------------------------------------------------------------
# Behavioural: Machine (including LargeBinary and nullable mac_address)
# ---------------------------------------------------------------------------


def _machine(name: str = "pc-1", mac: str | None = None) -> Machine:
    return Machine(
        name=name,
        ssh_host="192.168.1.10",
        ssh_port=22,
        ssh_user="dvorkam",
        ssh_key_encrypted=b"\x00\x01\x02",
        key_fingerprint="SHA256:abc",
        agent_port=36717,
        scripts_dir="/home/dvorkam/.csl/scripts",
        platform="linux",
        mac_address=mac,
    )


async def test_machine_insert(db_session: AsyncSession) -> None:
    db_session.add(_machine(mac="AA:BB:CC:DD:EE:FF"))
    await db_session.commit()

    result = await db_session.execute(select(Machine).where(Machine.name == "pc-1"))
    m = result.scalar_one()
    assert m.ssh_host == "192.168.1.10"
    assert m.ssh_key_encrypted == b"\x00\x01\x02"
    assert m.mac_address == "AA:BB:CC:DD:EE:FF"


async def test_machine_null_mac_address(db_session: AsyncSession) -> None:
    db_session.add(_machine(mac=None))
    await db_session.commit()

    result = await db_session.execute(select(Machine).where(Machine.name == "pc-1"))
    m = result.scalar_one()
    assert m.mac_address is None


# ---------------------------------------------------------------------------
# Behavioural: UserMachine bookmark
# ---------------------------------------------------------------------------


async def test_user_machine_insert(db_session: AsyncSession) -> None:
    user = User(username="dave", password_hash="h", role="user")
    machine = _machine(name="nas")
    db_session.add_all([user, machine])
    await db_session.commit()

    bm = UserMachine(user_id=user.id, machine_id=machine.id)
    db_session.add(bm)
    await db_session.commit()

    result = await db_session.execute(select(UserMachine).where(UserMachine.user_id == user.id))
    fetched = result.scalar_one()
    assert fetched.machine_id == machine.id


# ---------------------------------------------------------------------------
# Behavioural: Script (nullable meta_yaml)
# ---------------------------------------------------------------------------


def _script(user_id: int, name: str = "hello", meta: str | None = None) -> Script:
    return Script(
        name=name,
        content="#!/bin/bash\necho hi",
        meta_yaml=meta,
        md5="d41d8cd9",
        persistent=False,
        updated_at=datetime(2026, 1, 1),
        updated_by=user_id,
    )


async def test_script_insert_with_meta(db_session: AsyncSession) -> None:
    user = User(username="eve", password_hash="h", role="admin")
    db_session.add(user)
    await db_session.commit()

    db_session.add(_script(user.id, meta="description: hi\n"))
    await db_session.commit()

    result = await db_session.execute(select(Script).where(Script.name == "hello"))
    s = result.scalar_one()
    assert s.meta_yaml == "description: hi\n"
    assert s.persistent is False


async def test_script_insert_without_meta(db_session: AsyncSession) -> None:
    user = User(username="frank", password_hash="h", role="admin")
    db_session.add(user)
    await db_session.commit()

    db_session.add(_script(user.id, meta=None))
    await db_session.commit()

    result = await db_session.execute(select(Script).where(Script.name == "hello"))
    s = result.scalar_one()
    assert s.meta_yaml is None


# ---------------------------------------------------------------------------
# Behavioural: ScriptTargetState (composite PK, nullable md5 fields)
# ---------------------------------------------------------------------------


async def test_script_target_state_insert(db_session: AsyncSession) -> None:
    user = User(username="gina", password_hash="h", role="admin")
    machine = _machine(name="target")
    db_session.add_all([user, machine])
    await db_session.commit()

    script = _script(user.id, name="deploy")
    db_session.add(script)
    await db_session.commit()

    sts = ScriptTargetState(
        machine_id=machine.id,
        script_id=script.id,
        state="approved",
        approved_md5="abc123",
        pending_md5=None,
        last_refreshed_at=datetime(2026, 1, 1),
    )
    db_session.add(sts)
    await db_session.commit()

    result = await db_session.execute(
        select(ScriptTargetState).where(ScriptTargetState.machine_id == machine.id)
    )
    fetched = result.scalar_one()
    assert fetched.state == "approved"
    assert fetched.approved_md5 == "abc123"
    assert fetched.pending_md5 is None


# ---------------------------------------------------------------------------
# Behavioural: Job (nullable script_id, ended_at, exit_code, log_path)
# ---------------------------------------------------------------------------


async def test_job_insert_all_fields(db_session: AsyncSession) -> None:
    user = User(username="han", password_hash="h", role="user")
    machine = _machine(name="runner")
    db_session.add_all([user, machine])
    await db_session.commit()

    script = _script(user.id, name="task")
    db_session.add(script)
    await db_session.commit()

    job = Job(
        job_uuid=str(uuid4()),
        machine_id=machine.id,
        script_id=script.id,
        built_in_action=None,
        user_id=user.id,
        params_json="{}",
        status="completed",
        persistent=False,
        started_at=datetime(2026, 1, 1, 10, 0),
        ended_at=datetime(2026, 1, 1, 10, 1),
        exit_code=0,
        log_path="/home/dvorkam/.csl/logs/job.log",
    )
    db_session.add(job)
    await db_session.commit()

    result = await db_session.execute(select(Job).where(Job.status == "completed"))
    fetched = result.scalar_one()
    assert fetched.exit_code == 0
    assert fetched.script_id == script.id


async def test_job_nullable_fields(db_session: AsyncSession) -> None:
    user = User(username="ivy", password_hash="h", role="user")
    machine = _machine(name="runner2")
    db_session.add_all([user, machine])
    await db_session.commit()

    job = Job(
        job_uuid=str(uuid4()),
        machine_id=machine.id,
        script_id=None,
        built_in_action="wol",
        user_id=user.id,
        params_json="{}",
        status="running",
        persistent=False,
        started_at=datetime(2026, 1, 1, 10, 0),
        ended_at=None,
        exit_code=None,
        log_path=None,
    )
    db_session.add(job)
    await db_session.commit()

    result = await db_session.execute(select(Job).where(Job.built_in_action == "wol"))
    fetched = result.scalar_one()
    assert fetched.script_id is None
    assert fetched.ended_at is None
    assert fetched.exit_code is None
    assert fetched.log_path is None


# ---------------------------------------------------------------------------
# Behavioural: AuditLog (nullable user_id for system events)
# ---------------------------------------------------------------------------


async def test_audit_log_with_user(db_session: AsyncSession) -> None:
    user = User(username="jake", password_hash="h", role="admin")
    db_session.add(user)
    await db_session.commit()

    entry = AuditLog(
        user_id=user.id,
        action="script.run",
        target_type="script",
        target_id="hello",
        result="success",
        details_json='{"script": "hello"}',
    )
    db_session.add(entry)
    await db_session.commit()

    result = await db_session.execute(select(AuditLog).where(AuditLog.action == "script.run"))
    fetched = result.scalar_one()
    assert fetched.user_id == user.id
    assert fetched.result == "success"
    assert isinstance(fetched.timestamp, datetime)


async def test_audit_log_null_user_id_system_event(db_session: AsyncSession) -> None:
    entry = AuditLog(
        user_id=None,
        action="system.startup",
        target_type="system",
        target_id="server",
        result="success",
        details_json=None,
    )
    db_session.add(entry)
    await db_session.commit()

    result = await db_session.execute(select(AuditLog).where(AuditLog.action == "system.startup"))
    fetched = result.scalar_one()
    assert fetched.user_id is None
    assert fetched.details_json is None
