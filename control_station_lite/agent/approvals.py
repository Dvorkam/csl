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

import json
import logging
import threading
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from control_station_lite.agent.paths import CslPaths
from control_station_lite.shared.models import ApprovalState, ScriptDescriptor

__all__ = ["ApprovalError", "ApprovalsManager"]

logger = logging.getLogger(__name__)
_audit = logging.getLogger("csl.agent.audit")

# ---------------------------------------------------------------------------
# Internal data model — persisted to approvals.json
# ---------------------------------------------------------------------------


class _ScriptRecord(BaseModel):
    state: ApprovalState
    approved_md5: str | None = None
    approved_at: datetime | None = None
    approved_via: Literal["cli", "auto"] | None = None
    pending_md5: str | None = None
    rejected_at: datetime | None = None


class _ApprovalsStore(BaseModel):
    scripts: dict[str, _ScriptRecord] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Public exception
# ---------------------------------------------------------------------------


class ApprovalError(RuntimeError):
    """Raised when a state-machine transition is not permitted."""


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class ApprovalsManager:
    """Manages the per-script approval state machine for one agent installation.

    All public methods are thread-safe.  File writes use a temp-then-rename
    strategy so approvals.json is never left in a partially-written state.

    Transitions:
        absent         ──stage──────────────► pending        (manual policy)
        absent         ──stage──────────────► approved       (auto-approve policy)
        pending        ──approve────────────► approved
        pending        ──reject─────────────► rejected
        pending        ──clear──────────────► absent
        approved       ──stage (new md5)────► update_pending
        approved       ──clear──────────────► absent
        update_pending ──approve─────────────► approved  (new version becomes canonical)
        update_pending ──reject──────────────► rejected
        update_pending ──clear───────────────► absent
        rejected       ──clear───────────────► absent
    """

    def __init__(
        self,
        paths: CslPaths,
        *,
        auto_approve_list: list[str] | None = None,
    ) -> None:
        self._paths = paths
        self._auto_approve = set(auto_approve_list or [])
        self._lock = threading.Lock()
        self._store = self._load()
        self._sig: tuple[int, int] = self._current_sig()

    # ------------------------------------------------------------------
    # Public read operations
    # ------------------------------------------------------------------

    def get_state(self, name: str) -> ScriptDescriptor:
        """Return the current approval descriptor for *name*.

        Returns an absent descriptor for scripts that have not been staged yet.
        """
        with self._lock:
            self._sync_from_disk()
            if name not in self._store.scripts:
                return ScriptDescriptor(name=name, state=ApprovalState.absent)
            return self._descriptor(name)

    def list_all(self) -> list[ScriptDescriptor]:
        """Return descriptors for all scripts known to this agent."""
        with self._lock:
            self._sync_from_disk()
            return [self._descriptor(n) for n in self._store.scripts]

    # ------------------------------------------------------------------
    # State-mutating operations
    # ------------------------------------------------------------------

    def stage(
        self,
        name: str,
        content: str,
        md5: str,
        meta_yaml: str | None = None,
    ) -> ApprovalState:
        """Stage *content* for *name* and return the resulting ApprovalState.

        If the script is already staged with the same MD5, the existing state
        is returned unchanged (idempotent).  Raises ApprovalError if the
        current state is `rejected` — the target owner must `clear` first.
        """
        with self._lock:
            record = self._store.scripts.get(name)
            current = record.state if record else ApprovalState.absent

            if current == ApprovalState.rejected:
                raise ApprovalError(
                    f"'{name}' is rejected; run 'csl-agent approvals clear {name}' first"
                )

            if current == ApprovalState.approved and record and record.approved_md5 == md5:
                logger.debug("stage %s: md5 matches approved version, no-op", name)
                return ApprovalState.approved

            if current in (ApprovalState.pending, ApprovalState.update_pending):
                if record and record.pending_md5 == md5:
                    logger.debug("stage %s: already staged with same md5, no-op", name)
                    return current

            # Write content to pending dir.
            self._paths.pending_dir.mkdir(parents=True, exist_ok=True)
            (self._paths.pending_dir / name).write_text(content, encoding="utf-8")
            if meta_yaml is not None:
                (self._paths.pending_dir / f"{name}.meta.yaml").write_text(
                    meta_yaml, encoding="utf-8"
                )

            auto = name in self._auto_approve
            now = datetime.now(UTC)

            if auto:
                # Promote directly to approved — bypass manual review.
                self._paths.scripts_dir.mkdir(parents=True, exist_ok=True)
                (self._paths.pending_dir / name).replace(self._paths.scripts_dir / name)
                meta_src = self._paths.pending_dir / f"{name}.meta.yaml"
                if meta_src.exists():
                    meta_src.replace(self._paths.scripts_dir / f"{name}.meta.yaml")

                new_record = _ScriptRecord(
                    state=ApprovalState.approved,
                    approved_md5=md5,
                    approved_at=now,
                    approved_via="auto",
                )
                new_state = ApprovalState.approved
            elif current == ApprovalState.approved and record:
                new_record = _ScriptRecord(
                    state=ApprovalState.update_pending,
                    approved_md5=record.approved_md5,
                    approved_at=record.approved_at,
                    approved_via=record.approved_via,
                    pending_md5=md5,
                )
                new_state = ApprovalState.update_pending
            else:
                new_record = _ScriptRecord(
                    state=ApprovalState.pending,
                    pending_md5=md5,
                )
                new_state = ApprovalState.pending

            self._store.scripts[name] = new_record
            self._save()
            self._audit("stage", name, from_state=current, to_state=new_state, auto=auto)
            return new_state

    def approve(self, name: str) -> None:
        """Approve the pending version of *name*.

        Valid from: pending, update_pending.
        """
        with self._lock:
            record = self._require_record(name)
            current = record.state

            if current not in (ApprovalState.pending, ApprovalState.update_pending):
                raise ApprovalError(
                    f"cannot approve '{name}': current state is '{current}'"
                    f" (expected pending or update_pending)"
                )

            pending_file = self._paths.pending_dir / name
            if not pending_file.exists():
                raise ApprovalError(f"pending file for '{name}' not found at {pending_file}")

            self._paths.scripts_dir.mkdir(parents=True, exist_ok=True)
            pending_file.replace(self._paths.scripts_dir / name)
            meta = self._paths.pending_dir / f"{name}.meta.yaml"
            if meta.exists():
                meta.replace(self._paths.scripts_dir / f"{name}.meta.yaml")

            self._store.scripts[name] = _ScriptRecord(
                state=ApprovalState.approved,
                approved_md5=record.pending_md5,
                approved_at=datetime.now(UTC),
                approved_via="cli",
            )
            self._save()
            self._audit("approve", name, from_state=current, to_state=ApprovalState.approved)

    def reject(self, name: str) -> None:
        """Reject the pending (or update-pending) version of *name*.

        Valid from: pending, update_pending.
        All copies of the script are removed; state becomes rejected.
        """
        with self._lock:
            record = self._require_record(name)
            current = record.state

            if current not in (ApprovalState.pending, ApprovalState.update_pending):
                raise ApprovalError(
                    f"cannot reject '{name}': current state is '{current}'"
                    f" (expected pending or update_pending)"
                )

            self._remove_files(name)
            self._store.scripts[name] = _ScriptRecord(
                state=ApprovalState.rejected,
                rejected_at=datetime.now(UTC),
            )
            self._save()
            self._audit("reject", name, from_state=current, to_state=ApprovalState.rejected)

    def clear(self, name: str) -> None:
        """Remove *name* entirely, resetting it to absent.

        Valid from any non-absent state.  Clears rejections so the script can be re-staged.
        """
        with self._lock:
            record = self._require_record(name)
            current = record.state

            self._remove_files(name)
            self._store.scripts.pop(name, None)
            self._save()
            self._audit("clear", name, from_state=current, to_state=ApprovalState.absent)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_record(self, name: str) -> _ScriptRecord:
        """Return the record for *name*, raising ApprovalError if not found."""
        record = self._store.scripts.get(name)
        if record is None:
            raise ApprovalError(f"'{name}' not found; use stage to register it first")
        return record

    def _descriptor(self, name: str) -> ScriptDescriptor:
        """Convert a known record to a ScriptDescriptor. Caller must ensure *name* is in store."""
        record = self._store.scripts[name]
        return ScriptDescriptor(
            name=name,
            state=record.state,
            approved_md5=record.approved_md5,
            pending_md5=record.pending_md5,
        )

    def _remove_files(self, name: str) -> None:
        for directory in (self._paths.scripts_dir, self._paths.pending_dir):
            for suffix in ("", ".meta.yaml"):
                f = directory / f"{name}{suffix}"
                if f.exists():
                    f.unlink()

    # NOTE: This manager treats approvals.json as the authoritative source of truth.
    # If the filesystem and JSON diverge (e.g. a file is manually deleted while the
    # JSON still records it as approved, or vice versa), the returned state will be
    # inconsistent with reality.  A future reconciliation pass on startup (analogous
    # to what state.py does for running.json) should detect and resolve such drift.

    def _current_sig(self) -> tuple[int, int]:
        """Cheap change signature for approvals.json: ``(mtime_ns, size)``.

        Comparing size alongside the modification time catches writes that land
        within the same coarse mtime tick (common on some filesystems, e.g. a
        rapid stage-then-approve), which a bare mtime check would miss — leaving
        the in-memory store stale.
        """
        try:
            st = self._paths.approvals_path.stat()
        except FileNotFoundError:
            return (0, 0)
        return (st.st_mtime_ns, st.st_size)

    def _sync_from_disk(self) -> None:
        """Reload store if approvals.json was modified by an external process (e.g. CLI).

        Must be called while holding self._lock.
        """
        sig = self._current_sig()
        if sig != self._sig:
            self._store = self._load()
            self._sig = sig

    def _load(self) -> _ApprovalsStore:
        if not self._paths.approvals_path.exists():
            return _ApprovalsStore()
        try:
            raw = json.loads(self._paths.approvals_path.read_text(encoding="utf-8"))
            return _ApprovalsStore.model_validate(raw)
        except Exception as exc:
            logger.warning("could not load approvals.json, starting fresh: %s", exc)
            return _ApprovalsStore()

    def _save(self) -> None:
        """Atomic write: write to a temp file, then rename into place."""
        self._paths.approvals_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._paths.approvals_path.with_suffix(".tmp")
        tmp.write_text(
            self._store.model_dump_json(indent=2, exclude_none=True),
            encoding="utf-8",
        )
        tmp.replace(self._paths.approvals_path)
        self._sig = self._current_sig()

    def _audit(self, action: str, name: str, **details: object) -> None:
        parts = " ".join(f"{k}={v}" for k, v in details.items())
        _audit.info("action=%s script=%s %s", action, name, parts)
