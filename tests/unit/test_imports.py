"""Verify the package skeleton is importable end-to-end."""

import importlib

MODULES = [
    "control_station_lite",
    "control_station_lite.server",
    "control_station_lite.server.main",
    "control_station_lite.server.api.health",
    "control_station_lite.server.api.auth",
    "control_station_lite.server.api.machines",
    "control_station_lite.server.api.scripts",
    "control_station_lite.server.api.jobs",
    "control_station_lite.server.api.builtin",
    "control_station_lite.server.api.audit",
    "control_station_lite.server.api.admin",
    "control_station_lite.server.auth.jwt",
    "control_station_lite.server.auth.password",
    "control_station_lite.server.auth.dependencies",
    "control_station_lite.server.core.ssh",
    "control_station_lite.server.core.agent_client",
    "control_station_lite.server.core.script_registry",
    "control_station_lite.server.core.script_sync",
    "control_station_lite.server.core.magic_packet",
    "control_station_lite.server.core.crypto",
    "control_station_lite.server.db.models",
    "control_station_lite.server.db.session",
    "control_station_lite.agent",
    "control_station_lite.agent.cli",
    "control_station_lite.agent.config",
    "control_station_lite.agent.service_installer",
    "control_station_lite.agent.process_manager",
    "control_station_lite.agent.script_runner",
    "control_station_lite.agent.log_stream",
    "control_station_lite.agent.lifecycle",
    "control_station_lite.agent.state",
    "control_station_lite.agent.approvals",
    "control_station_lite.shared.models",
    "control_station_lite.shared.script_meta",
    "control_station_lite.shared.registration",
]


def test_all_modules_importable() -> None:
    for module in MODULES:
        importlib.import_module(module)
