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

import base64
import binascii
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CSL_",
        env_file=".env",
        env_file_encoding="utf-8",
    )

    master_key_path: Path
    jwt_key_path: Path
    database_url: str = "sqlite+aiosqlite:///data/control-station.sqlite"
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"
    # Set the Secure flag on auth cookies. Default on (production runs behind
    # nginx/TLS); set CSL_COOKIE_SECURE=false for plain-HTTP localhost dev.
    cookie_secure: bool = True

    @field_validator("master_key_path", mode="after")
    @classmethod
    def _validate_master_key_path(cls, v: Path) -> Path:
        if not v.exists():
            raise ValueError(f"master key file not found: {v}")
        try:
            key_bytes = base64.b64decode(v.read_text().strip(), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"master key is not valid base64: {exc}") from exc
        if len(key_bytes) != 32:
            raise ValueError(f"master key must decode to exactly 32 bytes, got {len(key_bytes)}")
        return v

    @field_validator("jwt_key_path", mode="after")
    @classmethod
    def _validate_jwt_key_path(cls, v: Path) -> Path:
        if not v.exists():
            raise ValueError(f"JWT key file not found: {v}")
        if not v.read_bytes():
            raise ValueError("JWT key file is empty")
        return v

    @field_validator("log_level", mode="before")
    @classmethod
    def _validate_log_level(cls, v: object) -> object:
        if isinstance(v, str):
            upper = v.upper()
            if upper not in _VALID_LOG_LEVELS:
                raise ValueError(f"log_level must be one of {sorted(_VALID_LOG_LEVELS)}, got {v!r}")
            return upper
        return v

    def read_master_key(self) -> bytes:
        """Return the raw 32-byte AES master key."""
        return base64.b64decode(self.master_key_path.read_text().strip())

    def read_jwt_key(self) -> bytes:
        """Return the raw JWT signing key bytes."""
        return self.jwt_key_path.read_bytes()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # fields populated from env at runtime
