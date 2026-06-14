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

"""Admin web pages: script library, machine management, user management, audit log."""

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_station_lite.server.api.machines import (
    MachineOut,
    RegisterMachineIn,
    register_machine_from_input,
)
from control_station_lite.server.core.script_registry import (
    ScriptRegistryError,
    create_script,
    delete_script,
    get_script_or_raise,
    list_scripts,
    update_script,
)
from control_station_lite.server.db.models import AuditLog, Machine, User, UserMachine
from control_station_lite.server.db.session import get_session
from control_station_lite.server.web import templates
from control_station_lite.server.web.deps import (
    clear_flash,
    read_flash,
    redirect_with_flash,
    web_require_admin,
)

router = APIRouter(prefix="/admin", tags=["admin-web"])

_PAGE_SIZE = 50


# ---------------------------------------------------------------------------
# 8.8 / 8.9 — Script library + editor
# ---------------------------------------------------------------------------


@router.get("/scripts", include_in_schema=False)
async def admin_scripts(
    request: Request,
    user: User = Depends(web_require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    scripts = await list_scripts(session)
    resp = templates.TemplateResponse(
        request,
        "admin/scripts.html",
        {"user": user, "scripts": scripts, "flash": read_flash(request)},
    )
    clear_flash(resp)
    return resp


@router.get("/scripts/new", include_in_schema=False)
async def admin_script_new_form(
    request: Request,
    user: User = Depends(web_require_admin),
) -> Response:
    return templates.TemplateResponse(
        request,
        "admin/script_edit.html",
        {"user": user, "script": None, "error": None},
    )


@router.post("/scripts/new", include_in_schema=False)
async def admin_script_new_submit(
    request: Request,
    name: str = Form(...),
    content: str = Form(...),
    meta_yaml: str = Form(default=""),
    user: User = Depends(web_require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    meta: str | None = meta_yaml.strip() or None
    try:
        await create_script(
            name=name, content=content, meta_yaml=meta, user_id=user.id, session=session
        )
        await session.commit()
    except ScriptRegistryError as exc:
        return templates.TemplateResponse(
            request,
            "admin/script_edit.html",
            {
                "user": user,
                "script": None,
                "error": str(exc),
                "name": name,
                "content": content,
                "meta_yaml": meta_yaml,
            },
            status_code=422,
        )
    return redirect_with_flash("/admin/scripts", f"Script '{name}' created.", "success")


@router.get("/scripts/{name}/edit", include_in_schema=False)
async def admin_script_edit_form(
    name: str,
    request: Request,
    user: User = Depends(web_require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    try:
        script = await get_script_or_raise(name, session)
    except ScriptRegistryError:
        return RedirectResponse("/admin/scripts", status_code=303)
    return templates.TemplateResponse(
        request,
        "admin/script_edit.html",
        {"user": user, "script": script, "error": None},
    )


@router.post("/scripts/{name}/edit", include_in_schema=False)
async def admin_script_edit_submit(
    name: str,
    request: Request,
    content: str = Form(...),
    meta_yaml: str = Form(default=""),
    user: User = Depends(web_require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    meta: str | None = meta_yaml.strip() or None
    try:
        script = await update_script(
            name=name, content=content, meta_yaml=meta, user_id=user.id, session=session
        )
        await session.commit()
    except ScriptRegistryError as exc:
        # Re-fetch for template (may not exist if name is wrong, but guard anyway)
        try:
            script = await get_script_or_raise(name, session)
        except ScriptRegistryError:
            return RedirectResponse("/admin/scripts", status_code=303)
        return templates.TemplateResponse(
            request,
            "admin/script_edit.html",
            {"user": user, "script": script, "error": str(exc)},
            status_code=422,
        )
    return redirect_with_flash("/admin/scripts", f"Script '{script.name}' updated.", "success")


@router.post("/scripts/{name}/delete", include_in_schema=False)
async def admin_script_delete(
    name: str,
    user: User = Depends(web_require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    try:
        await delete_script(name, session)
        await session.commit()
    except ScriptRegistryError:
        return redirect_with_flash("/admin/scripts", f"Script '{name}' not found.", "error")
    return redirect_with_flash("/admin/scripts", f"Script '{name}' deleted.", "success")


# ---------------------------------------------------------------------------
# 8.10 — Machine management
# ---------------------------------------------------------------------------


@router.get("/machines", include_in_schema=False)
async def admin_machines(
    request: Request,
    user: User = Depends(web_require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    result = await session.execute(select(Machine).order_by(Machine.name))
    machines = list(result.scalars().all())
    resp = templates.TemplateResponse(
        request,
        "admin/machines.html",
        {"user": user, "machines": machines, "flash": read_flash(request)},
    )
    clear_flash(resp)
    return resp


@router.get("/machines/new", include_in_schema=False)
async def admin_machine_new_form(
    request: Request,
    user: User = Depends(web_require_admin),
) -> Response:
    return templates.TemplateResponse(
        request,
        "admin/machine_new.html",
        {"user": user, "error": None, "form": {"ssh_port": 22}},
    )


@router.post("/machines/new", include_in_schema=False)
async def admin_machine_new_submit(
    request: Request,
    bundle: str = Form(...),
    name: str = Form(...),
    ssh_host: str = Form(...),
    ssh_port: int = Form(default=22),
    ssh_user: str = Form(default=""),
    mac_address: str = Form(default=""),
    user: User = Depends(web_require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    reg = RegisterMachineIn(
        bundle=bundle.strip(),
        name=name.strip(),
        ssh_host=ssh_host.strip(),
        ssh_port=ssh_port,
        ssh_user=ssh_user.strip() or None,
        mac_address=mac_address.strip() or None,
    )
    try:
        machine = await register_machine_from_input(reg, user.id, session)
    except HTTPException as exc:
        # No explicit rollback needed: the helper commits only on success, and the
        # request-scoped session (get_session) rolls back any open read transaction
        # when it closes.
        return templates.TemplateResponse(
            request,
            "admin/machine_new.html",
            {
                "user": user,
                "error": exc.detail,
                "form": {
                    "bundle": bundle,
                    "name": name,
                    "ssh_host": ssh_host,
                    "ssh_port": ssh_port,
                    "ssh_user": ssh_user,
                    "mac_address": mac_address,
                },
            },
            status_code=422,
        )
    fingerprint = MachineOut.model_validate(machine).ssh_host_key_fingerprint
    return redirect_with_flash(
        "/admin/machines",
        f"Machine '{machine.name}' registered. Confirm this SSH host-key "
        f"fingerprint with the target owner out-of-band: {fingerprint}",
        "success",
    )


@router.post("/machines/{machine_id}/delete", include_in_schema=False)
async def admin_machine_delete(
    machine_id: int,
    user: User = Depends(web_require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    result = await session.execute(select(Machine).where(Machine.id == machine_id))
    machine = result.scalar_one_or_none()
    if machine is None:
        return redirect_with_flash("/admin/machines", "Machine not found.", "error")
    # Remove bookmarks first (FK)
    await session.execute(delete(UserMachine).where(UserMachine.machine_id == machine_id))
    await session.delete(machine)
    await session.commit()
    return redirect_with_flash("/admin/machines", f"Machine '{machine.name}' deleted.", "success")


# ---------------------------------------------------------------------------
# 8.11 — User management
# ---------------------------------------------------------------------------


@router.get("/users", include_in_schema=False)
async def admin_users(
    request: Request,
    user: User = Depends(web_require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    result = await session.execute(select(User).order_by(User.username))
    users = list(result.scalars().all())
    resp = templates.TemplateResponse(
        request,
        "admin/users.html",
        {"user": user, "users": users, "flash": read_flash(request)},
    )
    clear_flash(resp)
    return resp


@router.post("/users/{user_id}/toggle", include_in_schema=False)
async def admin_user_toggle(
    user_id: int,
    current_admin: User = Depends(web_require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    result = await session.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if target is None:
        return redirect_with_flash("/admin/users", "User not found.", "error")
    if target.id == current_admin.id:
        return redirect_with_flash("/admin/users", "Cannot disable your own account.", "error")
    target.disabled = not target.disabled
    await session.commit()
    state = "disabled" if target.disabled else "enabled"
    return redirect_with_flash("/admin/users", f"User '{target.username}' {state}.", "success")


@router.post("/users/{user_id}/role", include_in_schema=False)
async def admin_user_role(
    user_id: int,
    role: str = Form(...),
    current_admin: User = Depends(web_require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    if role not in ("admin", "user"):
        return redirect_with_flash("/admin/users", "Invalid role.", "error")
    result = await session.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if target is None:
        return redirect_with_flash("/admin/users", "User not found.", "error")
    if target.id == current_admin.id and role != "admin":
        return redirect_with_flash("/admin/users", "Cannot demote your own account.", "error")
    target.role = role
    await session.commit()
    return redirect_with_flash(
        "/admin/users", f"User '{target.username}' role set to '{role}'.", "success"
    )


# ---------------------------------------------------------------------------
# 8.12 — Audit log viewer
# ---------------------------------------------------------------------------


@router.get("/audit", include_in_schema=False)
async def admin_audit(
    request: Request,
    action: str | None = None,
    target_type: str | None = None,
    username: str | None = None,
    page: int = 1,
    user: User = Depends(web_require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    page = max(1, page)
    offset = (page - 1) * _PAGE_SIZE

    stmt = select(AuditLog, User.username).outerjoin(User, AuditLog.user_id == User.id)
    if action:
        stmt = stmt.where(AuditLog.action.contains(action))
    if target_type:
        stmt = stmt.where(AuditLog.target_type == target_type)
    if username:
        stmt = stmt.where(User.username == username)

    stmt = stmt.order_by(AuditLog.timestamp.desc()).offset(offset).limit(_PAGE_SIZE + 1)
    rows_raw = list((await session.execute(stmt)).all())
    has_next = len(rows_raw) > _PAGE_SIZE
    rows = rows_raw[:_PAGE_SIZE]

    # Distinct action/target_type values for filter dropdowns
    actions_res = await session.execute(
        select(AuditLog.action).distinct().order_by(AuditLog.action)
    )
    target_types_res = await session.execute(
        select(AuditLog.target_type).distinct().order_by(AuditLog.target_type)
    )

    resp = templates.TemplateResponse(
        request,
        "admin/audit.html",
        {
            "user": user,
            "rows": rows,
            "has_next": has_next,
            "page": page,
            "filter_action": action or "",
            "filter_target_type": target_type or "",
            "filter_username": username or "",
            "actions": [r[0] for r in actions_res.all()],
            "target_types": [r[0] for r in target_types_res.all()],
            "flash": read_flash(request),
        },
    )
    clear_flash(resp)
    return resp
