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

Subcommands: ``create-admin`` (interactive admin bootstrap) and ``seed-scripts``
(idempotently load the packaged built-in script catalogue). Further commands
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


def _cmd_seed_scripts(args: argparse.Namespace) -> None:  # noqa: ARG001
    asyncio.run(_seed_scripts())


async def _seed_scripts() -> None:
    from sqlalchemy import select

    from control_station_lite.server.core.builtin_scripts import seed_builtin_scripts
    from control_station_lite.server.db.models import User
    from control_station_lite.server.db.session import _session_factory

    factory = _session_factory()
    async with factory() as session:
        # Built-in rows need an owner; attribute them to the earliest admin.
        result = await session.execute(
            select(User.id).where(User.role == "admin").order_by(User.id).limit(1)
        )
        admin_id = result.scalar_one_or_none()
        if admin_id is None:
            print(
                "ERROR: no admin user found — run 'csl-admin create-admin' first",
                file=sys.stderr,
            )
            sys.exit(1)

        seeded = await seed_builtin_scripts(session, user_id=admin_id)
        await session.commit()

    print(f"Built-in scripts: {len(seeded.created)} added, {len(seeded.skipped)} already present.")
    for name in seeded.created:
        print(f"  + {name}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="csl-admin", description="control-station-lite admin")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("create-admin", help="Create an initial admin user interactively")
    sub.add_parser("seed-scripts", help="Load the packaged built-in script catalogue (idempotent)")
    args = parser.parse_args()
    if args.command == "create-admin":
        _cmd_create_admin(args)
    elif args.command == "seed-scripts":
        _cmd_seed_scripts(args)
