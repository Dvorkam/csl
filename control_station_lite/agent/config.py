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

from __future__ import annotations

import logging
import os
import platform
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_station_lite.shared._validation import validate_stripping_unknowns

__all__ = [
    "AgentConfig",
    "AgentSection",
    "ApprovalPolicySection",
    "ConfigError",
    "IdentitySection",
    "default_config_path",
    "load_config",
]

logger = logging.getLogger(__name__)


class ConfigError(ValueError):
    """Raised when config.yaml cannot be parsed or fails validation."""


def _csl_dir() -> Path:
    """Return the platform-appropriate base directory for agent data."""
    if platform.system() == "Windows":
        appdata = os.environ.get("APPDATA", "")
        return Path(appdata) / "control-station-lite"
    return Path.home() / ".csl"


def default_config_path() -> Path:
    return _csl_dir() / "config.yaml"


class AgentSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    listen_port: int = 47731
    idle_timeout_seconds: int = 300
    # csl_dir is the single root for all agent data.  Override it to relocate
    # everything at once; override individual paths for finer control.
    csl_dir: Path = Field(default_factory=_csl_dir)
    scripts_dir: Path = Field(default_factory=lambda: _csl_dir() / "scripts")
    pending_dir: Path = Field(default_factory=lambda: _csl_dir() / "scripts.pending")
    logs_dir: Path = Field(default_factory=lambda: _csl_dir() / "logs")
    state_path: Path = Field(default_factory=lambda: _csl_dir() / "agent" / "running.json")
    approvals_path: Path = Field(default_factory=lambda: _csl_dir() / "agent" / "approvals.json")

    @model_validator(mode="before")
    @classmethod
    def _derive_path_defaults(cls, data: object) -> object:
        """If csl_dir is set, derive any unset path fields from it."""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        csl_dir = Path(str(data.get("csl_dir", _csl_dir()))).expanduser()
        data["csl_dir"] = csl_dir
        _defaults: dict[str, Path] = {
            "scripts_dir": csl_dir / "scripts",
            "pending_dir": csl_dir / "scripts.pending",
            "logs_dir": csl_dir / "logs",
            "state_path": csl_dir / "agent" / "running.json",
            "approvals_path": csl_dir / "agent" / "approvals.json",
        }
        for field, default in _defaults.items():
            data[field] = Path(str(data[field])).expanduser() if field in data else default
        return data

    @model_validator(mode="after")
    def _expand_paths(self) -> AgentSection:
        """Expand ~ on all path fields (covers direct construction from kwargs)."""
        self.csl_dir = self.csl_dir.expanduser()
        self.scripts_dir = self.scripts_dir.expanduser()
        self.pending_dir = self.pending_dir.expanduser()
        self.logs_dir = self.logs_dir.expanduser()
        self.state_path = self.state_path.expanduser()
        self.approvals_path = self.approvals_path.expanduser()
        return self


class IdentitySection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Both fields are absent until `csl-agent init` has run.
    key_fingerprint: str | None = None
    hostname_hint: str | None = None


class ApprovalPolicySection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto_approve: list[str] = Field(default_factory=list)


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: AgentSection = Field(default_factory=AgentSection)
    identity: IdentitySection = Field(default_factory=IdentitySection)
    approval_policy: ApprovalPolicySection = Field(default_factory=ApprovalPolicySection)


def load_config(path: Path | None = None) -> AgentConfig:
    """Load agent config from *path* (default: platform app-data dir).

    If the file does not exist, a fully-defaulted AgentConfig is returned.
    Unknown fields are logged as warnings and stripped; real validation
    errors raise ConfigError.
    """
    config_path = path or default_config_path()

    if not config_path.exists():
        logger.debug("config.yaml not found at %s — using defaults", config_path)
        return AgentConfig()

    try:
        text = config_path.read_text(encoding="utf-8")
        raw = yaml.safe_load(text)
    except OSError as exc:
        raise ConfigError(f"cannot read config file: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in config file: {exc}") from exc

    if raw is None:
        return AgentConfig()

    if not isinstance(raw, dict):
        raise ConfigError("config.yaml must be a YAML mapping at the top level")

    try:
        return validate_stripping_unknowns(
            AgentConfig,
            raw,
            log_prefix="unknown field in config.yaml will be ignored",
        )
    except Exception as exc:
        raise ConfigError(str(exc)) from exc
