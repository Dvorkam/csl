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

"""Argparse wiring and CLI entry point for csl-agent."""

from __future__ import annotations

import argparse
import logging
import sys

from .cmd_approvals import dispatch_approvals
from .cmd_init import cmd_init
from .cmd_policy import dispatch_policy
from .cmd_setup import cmd_setup


def main() -> None:
    """Entry point for the csl-agent CLI (``csl-agent = "...cli:main"``)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)-8s %(message)s",
        stream=sys.stderr if sys.stderr is not None else None,
    )

    parser = argparse.ArgumentParser(
        prog="csl-agent",
        description="Control Station Lite — agent management",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    # --- init ---
    init_p = subparsers.add_parser(
        "init",
        help="First-time setup: create directories, generate SSH keys, install service",
    )
    init_p.add_argument(
        "--port",
        type=int,
        default=36717,
        metavar="PORT",
        help="Port the agent will listen on (default: 36717)",
    )

    # --- setup ---
    subparsers.add_parser(
        "setup",
        help="Check SSH daemon prerequisites and attempt automatic fixes",
    )

    # --- approvals ---
    approvals_p = subparsers.add_parser(
        "approvals",
        help="Review and manage script approval state",
    )
    ap_sub = approvals_p.add_subparsers(dest="approvals_cmd", metavar="SUBCOMMAND")
    ap_sub.add_parser("list", help="List all scripts and their approval states")

    def _name(p: argparse.ArgumentParser) -> None:
        p.add_argument("name", metavar="NAME", help="Script name")

    _name(ap_sub.add_parser("show", help="Print the pending script content for review"))
    _name(ap_sub.add_parser("diff", help="Diff approved vs pending (update_pending only)"))
    _name(ap_sub.add_parser("approve", help="Approve the pending version"))
    _name(ap_sub.add_parser("reject", help="Reject the pending version"))
    _name(ap_sub.add_parser("clear", help="Remove a script, resetting state to absent"))
    purge_p = ap_sub.add_parser(
        "purge", help="Remove orphaned entries whose backing files are missing"
    )
    purge_p.add_argument(
        "--dry-run", action="store_true", help="Print what would be removed without removing it"
    )

    # --- policy ---
    policy_p = subparsers.add_parser("policy", help="Manage the auto-approve policy")
    pol_sub = policy_p.add_subparsers(dest="policy_cmd", metavar="SUBCOMMAND")
    pol_sub.add_parser("show", help="Show the current auto-approve list")
    aa_p = pol_sub.add_parser("auto-approve", help="Add a script to the auto-approve list")
    aa_p.add_argument("name", metavar="NAME")
    man_p = pol_sub.add_parser("manual", help="Remove a script from the auto-approve list")
    man_p.add_argument("name", metavar="NAME")

    args = parser.parse_args()

    if args.command == "init":
        cmd_init(args)
    elif args.command == "setup":
        cmd_setup()
    elif args.command == "approvals":
        dispatch_approvals(args, approvals_p)
    elif args.command == "policy":
        dispatch_policy(args, policy_p)
    else:
        parser.print_help()
        sys.exit(1)
