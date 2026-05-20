# SPDX-License-Identifier: AGPL-3.0-or-later
#
# control-station-lite
# Copyright (C) 2026 Michal Dvořák
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version, with an additional permission for
# distribution through app stores (see LICENSE).

from datetime import datetime

from sqlalchemy import TIMESTAMP, Boolean, ForeignKey, Integer, LargeBinary, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(Text, unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, default=datetime.utcnow)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"))
    token_hash: Mapped[str] = mapped_column(Text)
    issued_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class Machine(Base):
    __tablename__ = "machines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    ssh_host: Mapped[str] = mapped_column(Text)
    ssh_port: Mapped[int] = mapped_column(Integer)
    ssh_user: Mapped[str] = mapped_column(Text)
    ssh_key_encrypted: Mapped[bytes] = mapped_column(LargeBinary)
    key_fingerprint: Mapped[str] = mapped_column(Text)
    agent_port: Mapped[int] = mapped_column(Integer)
    scripts_dir: Mapped[str] = mapped_column(Text)
    platform: Mapped[str] = mapped_column(Text)
    mac_address: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMP, default=datetime.utcnow)


class UserMachine(Base):
    __tablename__ = "user_machines"

    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), primary_key=True)
    machine_id: Mapped[int] = mapped_column(Integer, ForeignKey("machines.id"), primary_key=True)


class Script(Base):
    __tablename__ = "scripts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    content: Mapped[str] = mapped_column(Text)
    meta_yaml: Mapped[str | None] = mapped_column(Text)
    md5: Mapped[str] = mapped_column(Text)
    persistent: Mapped[bool] = mapped_column(Boolean)
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    updated_by: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"))


class ScriptTargetState(Base):
    __tablename__ = "script_target_state"

    machine_id: Mapped[int] = mapped_column(Integer, ForeignKey("machines.id"), primary_key=True)
    script_id: Mapped[int] = mapped_column(Integer, ForeignKey("scripts.id"), primary_key=True)
    state: Mapped[str] = mapped_column(Text)
    approved_md5: Mapped[str | None] = mapped_column(Text)
    pending_md5: Mapped[str | None] = mapped_column(Text)
    last_refreshed_at: Mapped[datetime] = mapped_column(TIMESTAMP)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_uuid: Mapped[str] = mapped_column(Text, unique=True)
    machine_id: Mapped[int] = mapped_column(Integer, ForeignKey("machines.id"))
    script_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("scripts.id"))
    built_in_action: Mapped[str | None] = mapped_column(Text)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"))
    params_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    persistent: Mapped[bool] = mapped_column(Boolean)
    started_at: Mapped[datetime] = mapped_column(TIMESTAMP)
    ended_at: Mapped[datetime | None] = mapped_column(TIMESTAMP)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    log_path: Mapped[str | None] = mapped_column(Text)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(TIMESTAMP, default=datetime.utcnow)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(Text)
    target_type: Mapped[str] = mapped_column(Text)
    target_id: Mapped[str] = mapped_column(Text)
    result: Mapped[str] = mapped_column(Text)
    details_json: Mapped[str | None] = mapped_column(Text)
