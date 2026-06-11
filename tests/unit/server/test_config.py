"""Unit tests for server/config.py (Settings + get_settings)."""

import base64
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from control_station_lite.server.config import Settings, get_settings

# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def key_files(tmp_path: Path) -> tuple[Path, Path]:
    """Write a valid master.key (32 bytes as base64) and jwt.key."""
    master = tmp_path / "master.key"
    jwt = tmp_path / "jwt.key"
    master.write_text(base64.b64encode(os.urandom(32)).decode())
    jwt.write_bytes(b"test-jwt-signing-key")
    return master, jwt


def make_settings(master: Path, jwt: Path, **overrides: object) -> Settings:
    """Instantiate Settings bypassing the .env file entirely."""
    return Settings(
        master_key_path=master,
        jwt_key_path=jwt,
        **overrides,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Happy-path: all fields load correctly
# ---------------------------------------------------------------------------


def test_valid_settings_fields(key_files: tuple[Path, Path]) -> None:
    master, jwt = key_files
    s = make_settings(master, jwt)
    assert s.master_key_path == master
    assert s.jwt_key_path == jwt


def test_defaults() -> None:
    # Test code-level defaults directly — avoids .env overriding them locally.
    fields = Settings.model_fields
    assert fields["database_url"].default == "sqlite+aiosqlite:///data/control-station.sqlite"
    assert fields["host"].default == "127.0.0.1"
    assert fields["port"].default == 8000
    assert fields["log_level"].default == "INFO"
    assert fields["cookie_secure"].default is True


def test_cookie_secure_override(key_files: tuple[Path, Path]) -> None:
    master, jwt = key_files
    assert make_settings(master, jwt, cookie_secure=False).cookie_secure is False
    assert make_settings(master, jwt, cookie_secure=True).cookie_secure is True


def test_explicit_overrides(key_files: tuple[Path, Path]) -> None:
    master, jwt = key_files
    s = make_settings(
        master,
        jwt,
        database_url="sqlite+aiosqlite:///tmp/test.sqlite",
        host="0.0.0.0",
        port=9000,
        log_level="debug",
    )
    assert s.database_url == "sqlite+aiosqlite:///tmp/test.sqlite"
    assert s.host == "0.0.0.0"
    assert s.port == 9000
    assert s.log_level == "DEBUG"  # normalised to uppercase


# ---------------------------------------------------------------------------
# master_key_path validation
# ---------------------------------------------------------------------------


def test_master_key_missing_file(tmp_path: Path, key_files: tuple[Path, Path]) -> None:
    _, jwt = key_files
    with pytest.raises(ValidationError, match="master key file not found"):
        make_settings(tmp_path / "no_such_file.key", jwt)


def test_master_key_not_base64(tmp_path: Path, key_files: tuple[Path, Path]) -> None:
    _, jwt = key_files
    bad = tmp_path / "master.key"
    bad.write_text("this is not base64!!!")
    with pytest.raises(ValidationError, match="not valid base64"):
        make_settings(bad, jwt)


def test_master_key_too_short(tmp_path: Path, key_files: tuple[Path, Path]) -> None:
    _, jwt = key_files
    short = tmp_path / "master.key"
    short.write_text(base64.b64encode(os.urandom(16)).decode())  # 16 bytes, not 32
    with pytest.raises(ValidationError, match="exactly 32 bytes"):
        make_settings(short, jwt)


def test_master_key_too_long(tmp_path: Path, key_files: tuple[Path, Path]) -> None:
    _, jwt = key_files
    long_ = tmp_path / "master.key"
    long_.write_text(base64.b64encode(os.urandom(48)).decode())  # 48 bytes, not 32
    with pytest.raises(ValidationError, match="exactly 32 bytes"):
        make_settings(long_, jwt)


def test_master_key_empty_file(tmp_path: Path, key_files: tuple[Path, Path]) -> None:
    _, jwt = key_files
    empty = tmp_path / "master.key"
    empty.write_text("")
    # Empty string -> b64decode returns b"", length 0 != 32
    with pytest.raises(ValidationError, match="exactly 32 bytes"):
        make_settings(empty, jwt)


# ---------------------------------------------------------------------------
# jwt_key_path validation
# ---------------------------------------------------------------------------


def test_jwt_key_missing_file(tmp_path: Path, key_files: tuple[Path, Path]) -> None:
    master, _ = key_files
    with pytest.raises(ValidationError, match="JWT key file not found"):
        make_settings(master, tmp_path / "no_such_file.key")


def test_jwt_key_empty_file(tmp_path: Path, key_files: tuple[Path, Path]) -> None:
    master, _ = key_files
    empty = tmp_path / "jwt.key"
    empty.write_bytes(b"")
    with pytest.raises(ValidationError, match="JWT key file is empty"):
        make_settings(master, empty)


# ---------------------------------------------------------------------------
# log_level validation and normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("level", ["DEBUG", "INFO", "WARNING", "ERROR"])
def test_log_level_valid_uppercase(level: str, key_files: tuple[Path, Path]) -> None:
    master, jwt = key_files
    s = make_settings(master, jwt, log_level=level)
    assert s.log_level == level


@pytest.mark.parametrize("level", ["debug", "info", "warning", "error"])
def test_log_level_normalised_to_uppercase(level: str, key_files: tuple[Path, Path]) -> None:
    master, jwt = key_files
    s = make_settings(master, jwt, log_level=level)
    assert s.log_level == level.upper()


def test_log_level_invalid(key_files: tuple[Path, Path]) -> None:
    master, jwt = key_files
    with pytest.raises(ValidationError, match="log_level must be one of"):
        make_settings(master, jwt, log_level="VERBOSE")


# ---------------------------------------------------------------------------
# read_master_key / read_jwt_key
# ---------------------------------------------------------------------------


def test_read_master_key_returns_32_bytes(key_files: tuple[Path, Path]) -> None:
    master, jwt = key_files
    s = make_settings(master, jwt)
    key = s.read_master_key()
    assert isinstance(key, bytes)
    assert len(key) == 32


def test_read_master_key_matches_file_content(key_files: tuple[Path, Path]) -> None:
    master, jwt = key_files
    expected = base64.b64decode(master.read_text().strip())
    s = make_settings(master, jwt)
    assert s.read_master_key() == expected


def test_read_jwt_key_returns_bytes(key_files: tuple[Path, Path]) -> None:
    master, jwt = key_files
    s = make_settings(master, jwt)
    assert isinstance(s.read_jwt_key(), bytes)


def test_read_jwt_key_matches_file_content(key_files: tuple[Path, Path]) -> None:
    master, jwt = key_files
    s = make_settings(master, jwt)
    assert s.read_jwt_key() == jwt.read_bytes()


# ---------------------------------------------------------------------------
# get_settings caching
# ---------------------------------------------------------------------------


def test_get_settings_returns_same_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    master = tmp_path / "master.key"
    jwt = tmp_path / "jwt.key"
    master.write_text(base64.b64encode(os.urandom(32)).decode())
    jwt.write_bytes(b"cached-jwt-key")

    monkeypatch.setenv("CSL_MASTER_KEY_PATH", str(master))
    monkeypatch.setenv("CSL_JWT_KEY_PATH", str(jwt))

    get_settings.cache_clear()
    try:
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2
    finally:
        get_settings.cache_clear()
