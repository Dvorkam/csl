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

"""csl-admin CLI — server-side administrative commands.

Currently only ``create-admin`` is implemented; further subcommands
(key rotation, DB maintenance) can be added here.
"""

import argparse
import asyncio
import getpass
import sys


def _cmd_create_admin(args: argparse.Namespace) -> None:  # noqa: ARG001
    asyncio.run(_create_admin())


async def _create_admin() -> None:
    from sqlalchemy import select

    from control_station_lite.server.auth.password import hash_password
    from control_station_lite.server.db.models import User
    from control_station_lite.server.db.session import _session_factory

    username = input("Admin username: ").strip()
    if not username:
        print("ERROR: username cannot be empty", file=sys.stderr)
        sys.exit(1)

    password = getpass.getpass("Password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("ERROR: passwords do not match", file=sys.stderr)
        sys.exit(1)
    if len(password) < 8:
        print("ERROR: password must be at least 8 characters", file=sys.stderr)
        sys.exit(1)

    factory = _session_factory()
    async with factory() as session:
        existing = await session.execute(select(User).where(User.username == username))
        if existing.scalar_one_or_none() is not None:
            print(f"ERROR: user {username!r} already exists", file=sys.stderr)
            sys.exit(1)

        user = User(
            username=username,
            password_hash=hash_password(password),
            role="admin",
            disabled=False,
        )
        session.add(user)
        await session.commit()

    print(f"Admin user {username!r} created.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="csl-admin", description="control-station-lite admin")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("create-admin", help="Create an initial admin user interactively")
    args = parser.parse_args()
    if args.command == "create-admin":
        _cmd_create_admin(args)
