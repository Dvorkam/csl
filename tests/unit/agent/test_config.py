import logging
from pathlib import Path

import pytest
import yaml

from control_station_lite.agent.config import (
    AgentConfig,
    AgentSection,
    ApprovalPolicySection,
    ConfigError,
    IdentitySection,
    default_config_path,
    load_config,
)

# ---------------------------------------------------------------------------
# default_config_path
# ---------------------------------------------------------------------------


class TestDefaultConfigPath:
    def test_returns_path_object(self) -> None:
        assert isinstance(default_config_path(), Path)

    def test_ends_with_config_yaml(self) -> None:
        assert default_config_path().name == "config.yaml"


# ---------------------------------------------------------------------------
# AgentSection defaults
# ---------------------------------------------------------------------------


class TestAgentSectionDefaults:
    def test_default_port(self) -> None:
        assert AgentSection().listen_port == 47731

    def test_default_idle_timeout(self) -> None:
        assert AgentSection().idle_timeout_seconds == 600

    def test_paths_are_expanded(self) -> None:
        s = AgentSection()
        assert "~" not in str(s.scripts_dir)
        assert s.scripts_dir.is_absolute()

    def test_path_fields_from_string(self) -> None:
        s = AgentSection(scripts_dir="~/.csl/custom")  # type: ignore[arg-type]
        assert "~" not in str(s.scripts_dir)
        assert s.scripts_dir.is_absolute()


# ---------------------------------------------------------------------------
# IdentitySection
# ---------------------------------------------------------------------------


class TestIdentitySection:
    def test_defaults_are_none(self) -> None:
        i = IdentitySection()
        assert i.key_fingerprint is None
        assert i.hostname_hint is None

    def test_populated(self) -> None:
        i = IdentitySection(key_fingerprint="SHA256:abc", hostname_hint="my-pc")
        assert i.key_fingerprint == "SHA256:abc"


# ---------------------------------------------------------------------------
# ApprovalPolicySection
# ---------------------------------------------------------------------------


class TestApprovalPolicySection:
    def test_default_empty(self) -> None:
        assert ApprovalPolicySection().auto_approve == []

    def test_with_entries(self) -> None:
        p = ApprovalPolicySection(auto_approve=["sleep_machine", "restart_machine"])
        assert len(p.auto_approve) == 2


# ---------------------------------------------------------------------------
# AgentConfig defaults
# ---------------------------------------------------------------------------


class TestAgentConfigDefaults:
    def test_all_sections_present(self) -> None:
        cfg = AgentConfig()
        assert isinstance(cfg.agent, AgentSection)
        assert isinstance(cfg.identity, IdentitySection)
        assert isinstance(cfg.approval_policy, ApprovalPolicySection)


# ---------------------------------------------------------------------------
# load_config — missing file returns defaults
# ---------------------------------------------------------------------------


class TestLoadConfigMissingFile:
    def test_nonexistent_path_returns_defaults(self, tmp_path: Path) -> None:
        cfg = load_config(tmp_path / "nonexistent.yaml")
        assert cfg.agent.listen_port == 47731
        assert cfg.identity.key_fingerprint is None
        assert cfg.approval_policy.auto_approve == []


# ---------------------------------------------------------------------------
# load_config — valid full config
# ---------------------------------------------------------------------------


class TestLoadConfigValid:
    def test_full_config(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "agent": {
                        "listen_port": 9999,
                        "idle_timeout_seconds": 60,
                        "scripts_dir": str(tmp_path / "scripts"),
                        "pending_dir": str(tmp_path / "scripts.pending"),
                        "logs_dir": str(tmp_path / "logs"),
                        "state_path": str(tmp_path / "agent" / "running.json"),
                        "approvals_path": str(tmp_path / "agent" / "approvals.json"),
                    },
                    "identity": {
                        "key_fingerprint": "SHA256:abc123",
                        "hostname_hint": "gaming-pc",
                    },
                    "approval_policy": {
                        "auto_approve": ["sleep_machine"],
                    },
                }
            ),
            encoding="utf-8",
        )
        cfg = load_config(config_file)
        assert cfg.agent.listen_port == 9999
        assert cfg.agent.idle_timeout_seconds == 60
        assert cfg.identity.key_fingerprint == "SHA256:abc123"
        assert cfg.identity.hostname_hint == "gaming-pc"
        assert cfg.approval_policy.auto_approve == ["sleep_machine"]

    def test_partial_config_uses_defaults_for_missing(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("agent:\n  listen_port: 1234\n", encoding="utf-8")
        cfg = load_config(config_file)
        assert cfg.agent.listen_port == 1234
        assert cfg.agent.idle_timeout_seconds == 600
        assert cfg.identity.key_fingerprint is None

    def test_empty_file_returns_defaults(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("", encoding="utf-8")
        cfg = load_config(config_file)
        assert cfg.agent.listen_port == 47731

    def test_paths_expanded(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("agent:\n  scripts_dir: ~/.csl/scripts\n", encoding="utf-8")
        cfg = load_config(config_file)
        assert "~" not in str(cfg.agent.scripts_dir)


# ---------------------------------------------------------------------------
# load_config — validation integration
# ---------------------------------------------------------------------------
# validate_stripping_unknowns behaviour is tested in test_validation.py.
# Here we verify load_config wires it correctly and wraps errors as ConfigError.


class TestLoadConfigValidationIntegration:
    def test_unknown_field_warned_and_config_returned(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("typo_section:\n  x: 1\n", encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            cfg = load_config(config_file)
        assert any("typo_section" in r.message for r in caplog.records)
        assert cfg.agent.listen_port == 47731

    def test_invalid_yaml_raises_config_error(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("key: [unclosed", encoding="utf-8")
        with pytest.raises(ConfigError, match="invalid YAML"):
            load_config(config_file)

    def test_non_mapping_yaml_raises_config_error(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("- item\n", encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(config_file)

    def test_wrong_type_raises_config_error(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.yaml"
        config_file.write_text("agent:\n  listen_port: not_a_number\n", encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(config_file)
