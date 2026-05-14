import textwrap

import pytest

from control_station_lite.shared.script_meta import (
    ParamDescriptor,
    ParamType,
    ScriptMeta,
    ScriptMetaError,
    parse_meta_yaml,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def dedent(text: str) -> str:
    return textwrap.dedent(text).strip()


# ---------------------------------------------------------------------------
# parse_meta_yaml — happy paths
# ---------------------------------------------------------------------------


class TestParseMetaYamlHappy:
    def test_empty_yaml_returns_defaults(self) -> None:
        meta = parse_meta_yaml("")
        assert meta.description == ""
        assert meta.persistent is False
        assert meta.tags == []
        assert meta.params == []

    def test_minimal_description_only(self) -> None:
        meta = parse_meta_yaml("description: A simple script.")
        assert meta.description == "A simple script."

    def test_persistent_flag(self) -> None:
        meta = parse_meta_yaml("persistent: true")
        assert meta.persistent is True

    def test_tags(self) -> None:
        meta = parse_meta_yaml(
            dedent("""
            tags:
              - llm
              - dev-tools
        """)
        )
        assert meta.tags == ["llm", "dev-tools"]

    def test_full_schema_example(self) -> None:
        yaml_text = dedent("""
            description: |
              Multi-line description.
              Markdown supported.
            persistent: false
            tags:
              - llm
              - dev-tools
            params:
              - name: model_path
                type: string
                required: true
                help: "Filesystem path to the GGUF model file."
              - name: context_size
                type: int
                default: 4096
                min: 512
                max: 32768
                help: "Context window size."
              - name: gpu_layers
                type: choice
                choices: [0, 16, 32, "all"]
                default: "all"
                help: "Number of layers to offload to GPU."
        """)
        meta = parse_meta_yaml(yaml_text)
        assert len(meta.params) == 3
        assert meta.params[0].name == "model_path"
        assert meta.params[0].type == ParamType.string
        assert meta.params[0].required is True
        assert meta.params[1].min == 512
        assert meta.params[1].max == 32768
        assert meta.params[2].choices == [0, 16, 32, "all"]
        assert meta.params[2].default == "all"

    def test_bool_param(self) -> None:
        meta = parse_meta_yaml(
            dedent("""
            params:
              - name: verbose
                type: bool
                default: false
        """)
        )
        assert meta.params[0].type == ParamType.bool

    def test_float_param_with_bounds(self) -> None:
        meta = parse_meta_yaml(
            dedent("""
            params:
              - name: temperature
                type: float
                default: 0.7
                min: 0.0
                max: 2.0
        """)
        )
        assert meta.params[0].min == 0.0
        assert meta.params[0].max == 2.0

    def test_path_param(self) -> None:
        meta = parse_meta_yaml(
            dedent("""
            params:
              - name: output_dir
                type: path
                required: true
        """)
        )
        assert meta.params[0].type == ParamType.path


# ---------------------------------------------------------------------------
# parse_meta_yaml — unknown fields (shared behaviour via _validation.py)
# ---------------------------------------------------------------------------
# The validate_stripping_unknowns logic is tested exhaustively in
# tests/unit/shared/test_validation.py.  Here we only verify that
# parse_meta_yaml integrates it correctly (warning emitted, model usable).


class TestUnknownFieldsIntegration:
    def test_unknown_field_warned_and_model_still_returned(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING"):
            meta = parse_meta_yaml("description: ok\nunknown_field: bad")
        assert any("unknown_field" in r.message for r in caplog.records)
        assert meta.description == "ok"
        assert not hasattr(meta, "unknown_field")

    def test_unknown_field_with_real_error_raises_script_meta_error(self) -> None:
        yaml_text = dedent("""
            unknown_field: bad
            params:
              - name: broken
                type: not_a_real_type
                required: true
        """)
        with pytest.raises(ScriptMetaError):
            parse_meta_yaml(yaml_text)


# ---------------------------------------------------------------------------
# parse_meta_yaml — constraint validation
# ---------------------------------------------------------------------------


class TestConstraintValidation:
    def test_choice_without_choices_raises(self) -> None:
        yaml_text = dedent("""
            params:
              - name: mode
                type: choice
        """)
        with pytest.raises(ScriptMetaError, match="choices"):
            parse_meta_yaml(yaml_text)

    def test_min_on_string_param_raises(self) -> None:
        yaml_text = dedent("""
            params:
              - name: label
                type: string
                required: true
                min: 0
        """)
        with pytest.raises(ScriptMetaError, match="min"):
            parse_meta_yaml(yaml_text)

    def test_max_on_bool_param_raises(self) -> None:
        yaml_text = dedent("""
            params:
              - name: flag
                type: bool
                default: false
                max: 1
        """)
        with pytest.raises(ScriptMetaError, match="max"):
            parse_meta_yaml(yaml_text)

    def test_required_with_default_emits_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        yaml_text = dedent("""
            params:
              - name: size
                type: int
                required: true
                default: 10
        """)
        with caplog.at_level("WARNING", logger="control_station_lite.shared.script_meta"):
            parse_meta_yaml(yaml_text)
        assert any("default" in r.message for r in caplog.records)

    def test_optional_without_default_raises(self) -> None:
        yaml_text = dedent("""
            params:
              - name: size
                type: int
        """)
        with pytest.raises(ScriptMetaError, match="default"):
            parse_meta_yaml(yaml_text)

    def test_duplicate_param_names_raises(self) -> None:
        yaml_text = dedent("""
            params:
              - name: foo
                type: string
                required: true
              - name: foo
                type: int
                required: true
        """)
        with pytest.raises(ScriptMetaError, match="duplicate"):
            parse_meta_yaml(yaml_text)

    def test_invalid_param_type_raises(self) -> None:
        yaml_text = dedent("""
            params:
              - name: x
                type: bigint
                required: true
        """)
        with pytest.raises(ScriptMetaError):
            parse_meta_yaml(yaml_text)


# ---------------------------------------------------------------------------
# parse_meta_yaml — malformed YAML
# ---------------------------------------------------------------------------


class TestMalformedYaml:
    def test_invalid_yaml_syntax(self) -> None:
        with pytest.raises(ScriptMetaError, match="invalid YAML"):
            parse_meta_yaml("key: [unclosed")

    def test_non_mapping_top_level(self) -> None:
        with pytest.raises(ScriptMetaError):
            parse_meta_yaml("- item1\n- item2")


# ---------------------------------------------------------------------------
# Model direct construction
# ---------------------------------------------------------------------------


class TestParamDescriptorDirect:
    def test_choice_with_choices_ok(self) -> None:
        p = ParamDescriptor(name="mode", type=ParamType.choice, choices=["a", "b"], default="a")
        assert p.choices == ["a", "b"]

    def test_int_with_bounds_ok(self) -> None:
        p = ParamDescriptor(name="n", type=ParamType.int, min=1, max=100, default=10)
        assert p.min == 1

    def test_optional_param_requires_default(self) -> None:
        with pytest.raises(Exception, match="default"):
            ParamDescriptor(name="n", type=ParamType.int)


class TestScriptMetaDirect:
    def test_empty_is_valid(self) -> None:
        m = ScriptMeta()
        assert m.params == []

    def test_extra_field_forbidden_at_model_level(self) -> None:
        from pydantic import ValidationError as PydanticValidationError

        with pytest.raises(PydanticValidationError):
            ScriptMeta.model_validate({"surprise": True})
