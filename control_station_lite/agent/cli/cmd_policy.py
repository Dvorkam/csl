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

"""csl-agent policy — manage the auto-approve list in config.yaml."""

from __future__ import annotations

import argparse
import logging
import sys

from control_station_lite.agent.config import default_config_path

__all__ = [
    "cmd_policy_auto_approve",
    "cmd_policy_manual",
    "cmd_policy_show",
    "dispatch_policy",
]

logger = logging.getLogger(__name__)


def cmd_policy_show(_args: argparse.Namespace) -> None:
    from control_station_lite.agent.config import load_config

    cfg = load_config()
    names = cfg.approval_policy.auto_approve
    if not names:
        print("Auto-approve list is empty. All scripts require manual approval.")
    else:
        print("Scripts on the auto-approve list:")
        for name in sorted(names):
            print(f"  {name}")


def cmd_policy_auto_approve(args: argparse.Namespace) -> None:
    _policy_set_entry(args.name, add=True)


def cmd_policy_manual(args: argparse.Namespace) -> None:
    _policy_set_entry(args.name, add=False)


def _policy_set_entry(name: str, *, add: bool) -> None:
    """Add or remove *name* from the auto-approve list in config.yaml."""
    import yaml

    from control_station_lite.agent.config import load_config

    config_path = default_config_path()
    cfg = load_config()
    current: list[str] = list(cfg.approval_policy.auto_approve)

    if add:
        if name in current:
            print(f"'{name}' is already on the auto-approve list.")
            return
        current.append(name)
        verb = "added to"
    else:
        if name not in current:
            print(f"'{name}' is not on the auto-approve list.")
            return
        current.remove(name)
        verb = "removed from"

    raw: dict[str, object] = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    policy: dict[str, object] = raw.setdefault("approval_policy", {})  # type: ignore[assignment]
    policy["auto_approve"] = current
    config_path.write_text(
        yaml.dump(raw, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )
    print(f"'{name}' {verb} the auto-approve list.")


def dispatch_policy(args: argparse.Namespace, parent: argparse.ArgumentParser) -> None:
    fn = {
        "show": cmd_policy_show,
        "auto-approve": cmd_policy_auto_approve,
        "manual": cmd_policy_manual,
    }.get(args.policy_cmd or "")
    if fn is None:
        parent.print_help()
        sys.exit(1)
    fn(args)
