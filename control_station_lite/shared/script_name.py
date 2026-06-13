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

"""Validation for script names that become on-disk filenames.

A script name is written to the agent's ``scripts.pending/`` and ``scripts/``
directories verbatim (plus a ``.meta.yaml`` sibling), so it must be a safe
filename on every supported OS — including Windows, whose reserved device names
and trailing-dot rules would otherwise turn a write into a ``PermissionError``
(a 500) or, worse, escape the directory.

Used by the agent endpoints that take a ``{name}`` path parameter; centralised
here so the server can apply the same rule.
"""

from __future__ import annotations

import re

__all__ = ["MAX_SCRIPT_NAME_LENGTH", "ScriptNameError", "validate_script_name"]

MAX_SCRIPT_NAME_LENGTH = 100

# Allowed characters: ASCII letters/digits plus ``_ - .`` — no path separators,
# whitespace, or shell/Windows-reserved punctuation (`< > : " | ? *` etc.).
_ALLOWED = re.compile(r"^[A-Za-z0-9_.\-]+$")

# Windows reserved device names. Reserved regardless of extension, so the check
# compares against the stem (the part before the first dot), case-insensitively.
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


class ScriptNameError(ValueError):
    """Raised when a script name is not a safe cross-platform filename."""


def validate_script_name(name: str) -> str:
    """Return *name* unchanged if it is a safe filename, else raise.

    Raises:
        ScriptNameError: empty, too long, illegal characters, only dots,
            a trailing dot, or a Windows reserved device name.
    """
    if not name:
        raise ScriptNameError("script name must not be empty")
    if len(name) > MAX_SCRIPT_NAME_LENGTH:
        raise ScriptNameError(f"script name must be at most {MAX_SCRIPT_NAME_LENGTH} characters")
    if not _ALLOWED.match(name):
        raise ScriptNameError("script name may contain only letters, digits, '_', '-' and '.'")
    if set(name) <= {"."}:  # ".", "..", "..." — not a real filename
        raise ScriptNameError("script name must not consist only of dots")
    if name.endswith("."):  # Windows strips trailing dots, changing identity
        raise ScriptNameError("script name must not end with a dot")
    if name.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
        raise ScriptNameError("script name must not be a reserved device name")
    return name
