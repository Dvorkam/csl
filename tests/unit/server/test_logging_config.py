"""Unit tests for structured JSON logging (Task 9.4)."""

import json
import logging

import pytest

from control_station_lite.server.logging_config import (
    JSONFormatter,
    configure_logging,
    request_id_var,
)


def _record(**kw) -> logging.LogRecord:
    defaults: dict = {
        "name": "csl.test",
        "level": logging.INFO,
        "pathname": __file__,
        "lineno": 1,
        "msg": "hello %s",
        "args": ("world",),
        "exc_info": None,
    }
    defaults.update(kw)
    return logging.LogRecord(func=None, **defaults)


def test_format_is_single_line_json() -> None:
    out = JSONFormatter().format(_record())
    assert "\n" not in out
    parsed = json.loads(out)
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "csl.test"
    assert parsed["message"] == "hello world"
    assert "timestamp" in parsed


def test_format_includes_request_id_when_set() -> None:
    token = request_id_var.set("req-123")
    try:
        parsed = json.loads(JSONFormatter().format(_record()))
    finally:
        request_id_var.reset(token)
    assert parsed["request_id"] == "req-123"


def test_format_omits_request_id_when_unset() -> None:
    parsed = json.loads(JSONFormatter().format(_record()))
    assert "request_id" not in parsed


def test_format_includes_extra_fields() -> None:
    rec = _record()
    rec.machine_id = 7
    parsed = json.loads(JSONFormatter().format(rec))
    assert parsed["machine_id"] == 7


def test_format_includes_exception() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        rec = _record(exc_info=sys.exc_info())
    parsed = json.loads(JSONFormatter().format(rec))
    assert "ValueError: boom" in parsed["exc_info"]


@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    yield
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


@pytest.mark.usefixtures("restore_logging")
def test_configure_logging_installs_single_json_handler() -> None:
    configure_logging("DEBUG")
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, JSONFormatter)
    assert root.level == logging.DEBUG
    # Idempotent: a second call does not stack handlers.
    configure_logging("INFO")
    assert len(root.handlers) == 1
