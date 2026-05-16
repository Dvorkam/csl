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

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_station_lite.shared._validation import validate_stripping_unknowns

__all__ = ["ParamDescriptor", "ParamType", "ScriptMeta", "ScriptMetaError", "parse_meta_yaml"]

logger = logging.getLogger(__name__)


class ScriptMetaError(ValueError):
    """Raised when a .meta.yaml file fails to parse or validate."""


class ParamType(StrEnum):
    string = "string"
    int = "int"
    float = "float"
    bool = "bool"
    choice = "choice"
    path = "path"


class ParamDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: ParamType
    required: bool = False
    help: str = ""
    default: Any = None
    # Numeric bounds — only valid for int / float params.
    min: float | None = None
    max: float | None = None
    # Allowed values — required when type == choice.
    choices: list[str | int | float] | None = None

    @model_validator(mode="after")
    def _validate_constraints(self) -> ParamDescriptor:
        if self.type == ParamType.choice and not self.choices:
            raise ValueError(f"param '{self.name}': 'choices' is required for type 'choice'")
        if self.type not in (ParamType.int, ParamType.float):
            if self.min is not None or self.max is not None:
                raise ValueError(
                    f"param '{self.name}': 'min'/'max' are only valid for int or float params"
                )
        if self.required and self.default is not None:
            # Logically contradictory but harmless — the default will simply be ignored.
            logger.warning(
                "param '%s': has both 'required: true' and a 'default';"
                " the default will be ignored",
                self.name,
            )
        if not self.required and self.default is None:
            raise ValueError(
                f"param '{self.name}': non-required params must define a 'default' value"
            )
        return self


class ScriptMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = ""
    persistent: bool = False
    tags: list[str] = Field(default_factory=list)
    params: list[ParamDescriptor] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_param_names(self) -> ScriptMeta:
        seen: set[str] = set()
        for p in self.params:
            if p.name in seen:
                raise ValueError(f"duplicate param name '{p.name}'")
            seen.add(p.name)
        return self


def parse_meta_yaml(text: str) -> ScriptMeta:
    """Parse and validate a .meta.yaml string, returning a sanitized ScriptMeta.

    The returned model is a *sanitized* version of the input: unknown fields are
    stripped before the model is constructed, so callers should not assume the
    result reflects the raw YAML one-for-one.

    Unknown fields are logged as warnings and silently dropped — a typo in a
    field name will not crash the caller.  All other validation failures
    (wrong types, missing required fields, constraint violations) raise
    ScriptMetaError immediately.
    """
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ScriptMetaError(f"invalid YAML: {exc}") from exc

    if raw is None:
        raw = {}

    if not isinstance(raw, dict):
        raise ScriptMetaError("meta.yaml must be a YAML mapping at the top level")

    try:
        return validate_stripping_unknowns(
            ScriptMeta,
            raw,
            log_prefix="unknown field in meta.yaml will be ignored",
        )
    except Exception as exc:
        raise ScriptMetaError(str(exc)) from exc
