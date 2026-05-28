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

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

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
from control_station_lite.server.web.deps import pop_flash, set_flash, web_require_admin

router = APIRouter(prefix="/admin", tags=["admin-web"])

_PAGE_SIZE = 50


# ---------------------------------------------------------------------------
# 8.8 / 8.9 — Script library + editor
# ---------------------------------------------------------------------------


@router.get("/scripts", include_in_schema=False)
async def admin_scripts(
    request: Request,
    response: Response,
    user: User = Depends(web_require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    scripts = await list_scripts(session)
    flash = pop_flash(request, response)
    return templates.TemplateResponse(
        request,
        "admin/scripts.html",
        {"user": user, "scripts": scripts, "flash": flash},
    )


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
    response: Response,
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
    set_flash(response, f"Script '{name}' created.", "success")
    return RedirectResponse("/admin/scripts", status_code=303)


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
    response: Response,
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
    set_flash(response, f"Script '{script.name}' updated.", "success")
    return RedirectResponse("/admin/scripts", status_code=303)


@router.post("/scripts/{name}/delete", include_in_schema=False)
async def admin_script_delete(
    name: str,
    response: Response,
    user: User = Depends(web_require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    try:
        await delete_script(name, session)
        await session.commit()
        set_flash(response, f"Script '{name}' deleted.", "success")
    except ScriptRegistryError:
        set_flash(response, f"Script '{name}' not found.", "error")
    return RedirectResponse("/admin/scripts", status_code=303)


# ---------------------------------------------------------------------------
# 8.10 — Machine management
# ---------------------------------------------------------------------------


@router.get("/machines", include_in_schema=False)
async def admin_machines(
    request: Request,
    response: Response,
    user: User = Depends(web_require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    result = await session.execute(select(Machine).order_by(Machine.name))
    machines = list(result.scalars().all())
    flash = pop_flash(request, response)
    return templates.TemplateResponse(
        request,
        "admin/machines.html",
        {"user": user, "machines": machines, "flash": flash},
    )


@router.post("/machines/{machine_id}/delete", include_in_schema=False)
async def admin_machine_delete(
    machine_id: int,
    response: Response,
    user: User = Depends(web_require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    result = await session.execute(select(Machine).where(Machine.id == machine_id))
    machine = result.scalar_one_or_none()
    if machine is None:
        set_flash(response, "Machine not found.", "error")
        return RedirectResponse("/admin/machines", status_code=303)
    # Remove bookmarks first (FK)
    await session.execute(delete(UserMachine).where(UserMachine.machine_id == machine_id))
    await session.delete(machine)
    await session.commit()
    set_flash(response, f"Machine '{machine.name}' deleted.", "success")
    return RedirectResponse("/admin/machines", status_code=303)


# ---------------------------------------------------------------------------
# 8.11 — User management
# ---------------------------------------------------------------------------


@router.get("/users", include_in_schema=False)
async def admin_users(
    request: Request,
    response: Response,
    user: User = Depends(web_require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    result = await session.execute(select(User).order_by(User.username))
    users = list(result.scalars().all())
    flash = pop_flash(request, response)
    return templates.TemplateResponse(
        request,
        "admin/users.html",
        {"user": user, "users": users, "flash": flash},
    )


@router.post("/users/{user_id}/toggle", include_in_schema=False)
async def admin_user_toggle(
    user_id: int,
    response: Response,
    current_admin: User = Depends(web_require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    result = await session.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if target is None:
        set_flash(response, "User not found.", "error")
        return RedirectResponse("/admin/users", status_code=303)
    if target.id == current_admin.id:
        set_flash(response, "Cannot disable your own account.", "error")
        return RedirectResponse("/admin/users", status_code=303)
    target.disabled = not target.disabled
    await session.commit()
    state = "disabled" if target.disabled else "enabled"
    set_flash(response, f"User '{target.username}' {state}.", "success")
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/role", include_in_schema=False)
async def admin_user_role(
    user_id: int,
    response: Response,
    role: str = Form(...),
    current_admin: User = Depends(web_require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    if role not in ("admin", "user"):
        set_flash(response, "Invalid role.", "error")
        return RedirectResponse("/admin/users", status_code=303)
    result = await session.execute(select(User).where(User.id == user_id))
    target = result.scalar_one_or_none()
    if target is None:
        set_flash(response, "User not found.", "error")
        return RedirectResponse("/admin/users", status_code=303)
    if target.id == current_admin.id and role != "admin":
        set_flash(response, "Cannot demote your own account.", "error")
        return RedirectResponse("/admin/users", status_code=303)
    target.role = role
    await session.commit()
    set_flash(response, f"User '{target.username}' role set to '{role}'.", "success")
    return RedirectResponse("/admin/users", status_code=303)


# ---------------------------------------------------------------------------
# 8.12 — Audit log viewer
# ---------------------------------------------------------------------------


@router.get("/audit", include_in_schema=False)
async def admin_audit(
    request: Request,
    response: Response,
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

    flash = pop_flash(request, response)
    return templates.TemplateResponse(
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
            "flash": flash,
        },
    )
