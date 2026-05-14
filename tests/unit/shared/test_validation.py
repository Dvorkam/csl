import logging

import pytest
from pydantic import BaseModel, ConfigDict

from control_station_lite.shared._validation import validate_stripping_unknowns


class _Inner(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int


class _Outer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = ""
    inner: _Inner | None = None


class TestValidateStrippingUnknowns:
    def test_valid_input_returns_model(self) -> None:
        result = validate_stripping_unknowns(_Outer, {"name": "ok"})
        assert result.name == "ok"

    def test_unknown_top_level_field_warned_and_stripped(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="control_station_lite.shared._validation"):
            result = validate_stripping_unknowns(_Outer, {"name": "ok", "surprise": 1})
        assert result.name == "ok"
        assert not hasattr(result, "surprise")
        assert any("surprise" in r.message for r in caplog.records)

    def test_unknown_nested_field_warned_and_stripped(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="control_station_lite.shared._validation"):
            result = validate_stripping_unknowns(
                _Outer, {"name": "ok", "inner": {"value": 1, "extra": "bad"}}
            )
        assert result.inner is not None
        assert result.inner.value == 1
        assert any("extra" in r.message for r in caplog.records)

    def test_real_error_reraises_validation_error(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            validate_stripping_unknowns(_Outer, {"inner": {"value": "not_an_int"}})

    def test_mixed_unknown_and_real_error_reraises(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            validate_stripping_unknowns(_Outer, {"surprise": 1, "inner": {"value": "not_an_int"}})

    def test_custom_log_prefix(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="control_station_lite.shared._validation"):
            validate_stripping_unknowns(
                _Outer, {"bad_key": 1}, log_prefix="config.yaml unknown field"
            )
        assert any("config.yaml unknown field" in r.message for r in caplog.records)

    def test_returned_model_never_contains_unknown_keys(self) -> None:
        result = validate_stripping_unknowns(_Outer, {"name": "x", "ghost": True})
        assert result.model_fields_set == {"name"}

    def test_empty_dict_returns_defaults(self) -> None:
        result = validate_stripping_unknowns(_Outer, {})
        assert result.name == ""
        assert result.inner is None

    def test_type_var_preserves_return_type(self) -> None:
        # Static check: result should be inferred as _Outer, not BaseModel.
        result = validate_stripping_unknowns(_Outer, {})
        assert isinstance(result, _Outer)

    def test_multiple_unknowns_all_warned(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger="control_station_lite.shared._validation"):
            validate_stripping_unknowns(_Outer, {"a": 1, "b": 2, "c": 3})
        warned = [r.message for r in caplog.records]
        assert sum(1 for m in warned if any(k in m for k in ("a", "b", "c"))) == 3
