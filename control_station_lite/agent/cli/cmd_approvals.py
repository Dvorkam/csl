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

"""csl-agent approvals — review and manage script approval state."""

from __future__ import annotations

import argparse
import difflib
import logging
import sys

__all__ = [
    "_load_approvals",
    "_state_badge",
    "cmd_approvals_approve",
    "cmd_approvals_clear",
    "cmd_approvals_diff",
    "cmd_approvals_list",
    "cmd_approvals_reject",
    "cmd_approvals_show",
    "dispatch_approvals",
]

logger = logging.getLogger(__name__)


def _load_approvals() -> tuple[object, object]:
    """Return (ApprovalsManager, AgentConfig) from the on-disk config."""
    from control_station_lite.agent.approvals import ApprovalsManager
    from control_station_lite.agent.config import load_config

    cfg = load_config()
    paths = cfg.agent.to_csl_paths()
    mgr = ApprovalsManager(paths, auto_approve_list=cfg.approval_policy.auto_approve)
    return mgr, cfg


def _state_badge(state: str) -> str:
    return {
        "absent": "absent",
        "pending": "PENDING (awaiting approval)",
        "approved": "approved",
        "update_pending": "UPDATE PENDING (new version staged)",
        "rejected": "REJECTED",
    }.get(state, state)


def cmd_approvals_list(_args: argparse.Namespace) -> None:
    mgr, _ = _load_approvals()
    from control_station_lite.agent.approvals import ApprovalsManager

    assert isinstance(mgr, ApprovalsManager)
    descriptors = mgr.list_all()
    if not descriptors:
        print("No scripts registered on this agent.")
        return
    col = max(len(d.name) for d in descriptors)
    print(f"{'SCRIPT':<{col}}  STATE")
    print("-" * (col + 2 + 30))
    for d in sorted(descriptors, key=lambda x: x.name):
        md5_info = ""
        if d.approved_md5:
            md5_info += f"  approved={d.approved_md5[:8]}"
        if d.pending_md5:
            md5_info += f"  pending={d.pending_md5[:8]}"
        print(f"{d.name:<{col}}  {_state_badge(d.state)}{md5_info}")


def cmd_approvals_show(args: argparse.Namespace) -> None:
    mgr, cfg = _load_approvals()
    from control_station_lite.agent.approvals import ApprovalsManager
    from control_station_lite.agent.config import AgentConfig

    assert isinstance(mgr, ApprovalsManager)
    assert isinstance(cfg, AgentConfig)
    name: str = args.name
    descriptor = mgr.get_state(name)
    state = str(descriptor.state)

    if state not in ("pending", "update_pending"):
        print(f"'{name}' is {_state_badge(state)} — nothing pending to show.", file=sys.stderr)
        sys.exit(1)

    pending_path = cfg.agent.to_csl_paths().pending_dir / name
    if not pending_path.exists():
        print(f"Pending file not found: {pending_path}", file=sys.stderr)
        sys.exit(1)

    print(f"=== Pending content of '{name}' ===")
    print(pending_path.read_text(encoding="utf-8"), end="")


def cmd_approvals_diff(args: argparse.Namespace) -> None:
    mgr, cfg = _load_approvals()
    from control_station_lite.agent.approvals import ApprovalsManager
    from control_station_lite.agent.config import AgentConfig

    assert isinstance(mgr, ApprovalsManager)
    assert isinstance(cfg, AgentConfig)
    name: str = args.name
    descriptor = mgr.get_state(name)
    state = str(descriptor.state)

    if state != "update_pending":
        print(
            f"'{name}' is {_state_badge(state)} — diff only available for update_pending scripts.",
            file=sys.stderr,
        )
        sys.exit(1)

    paths = cfg.agent.to_csl_paths()
    approved_lines = (
        (paths.scripts_dir / name).read_text(encoding="utf-8").splitlines(keepends=True)
    )
    pending_lines = (paths.pending_dir / name).read_text(encoding="utf-8").splitlines(keepends=True)
    diff = list(
        difflib.unified_diff(
            approved_lines,
            pending_lines,
            fromfile=f"approved/{name}",
            tofile=f"pending/{name}",
        )
    )
    if not diff:
        print("(files are identical)")
    else:
        print("".join(diff), end="")


def cmd_approvals_approve(args: argparse.Namespace) -> None:
    mgr, _ = _load_approvals()
    from control_station_lite.agent.approvals import ApprovalError, ApprovalsManager

    assert isinstance(mgr, ApprovalsManager)
    try:
        mgr.approve(args.name)
        print(f"✓ '{args.name}' approved.")
    except ApprovalError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


def cmd_approvals_reject(args: argparse.Namespace) -> None:
    mgr, _ = _load_approvals()
    from control_station_lite.agent.approvals import ApprovalError, ApprovalsManager

    assert isinstance(mgr, ApprovalsManager)
    try:
        mgr.reject(args.name)
        print(f"✗ '{args.name}' rejected.")
    except ApprovalError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


def cmd_approvals_clear(args: argparse.Namespace) -> None:
    mgr, _ = _load_approvals()
    from control_station_lite.agent.approvals import ApprovalError, ApprovalsManager

    assert isinstance(mgr, ApprovalsManager)
    try:
        mgr.clear(args.name)
        print(f"'{args.name}' removed (state reset to absent).")
    except ApprovalError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


def dispatch_approvals(args: argparse.Namespace, parent: argparse.ArgumentParser) -> None:
    fn = {
        "list": cmd_approvals_list,
        "show": cmd_approvals_show,
        "diff": cmd_approvals_diff,
        "approve": cmd_approvals_approve,
        "reject": cmd_approvals_reject,
        "clear": cmd_approvals_clear,
    }.get(args.approvals_cmd or "")
    if fn is None:
        parent.print_help()
        sys.exit(1)
    fn(args)
