"""API para la extensión de Chrome (autenticada con token)."""
from __future__ import annotations

import functools
from datetime import date, datetime, timedelta
from typing import Any, Optional, Tuple
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from flask import jsonify, make_response, request

from gtd.utils import _today_madrid, coerce_priority, normalize_name
from gtd.core import app
from gtd.db import commit, exec_sql, q, q1, rollback
from gtd.lookups import find_project_by_name_active, get_or_create_tag
from gtd.roles import role_tag
from gtd.kanban_board import _project_board_status_for_task
from gtd.recurrence import complete_recurring_task_run
from gtd.calendar_sync import _mark_task_calendar_dirty
from gtd.tasks_ops import complete_nonrecurring_task


# --- Decorador para requerir token API en endpoints de extensión ---
def require_api_token(view_func):
    @functools.wraps(view_func)
    def wrapped(*args, **kwargs):
        # Permitir token en Authorization: Bearer <token> o en ?token= o JSON body
        token = None
        auth_header = request.headers.get("Authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
        if not token:
            token = request.args.get("token")
        if not token and request.is_json:
            token = (request.get_json(silent=True) or {}).get("token")
        if not token:
            return make_response(jsonify({"ok": False, "error": "Token requerido"}), 401)
        row = q1("SELECT user_id, active FROM api_tokens WHERE token=%s", (token,))
        if not row or not row.get("active") or int(row.get("user_id", 0)) != 1:
            return make_response(jsonify({"ok": False, "error": "Token inválido o revocado"}), 401)
        # Opcional: actualizar last_used_at
        exec_sql("UPDATE api_tokens SET last_used_at=NOW() WHERE token=%s", (token,))
        commit()
        return view_func(*args, **kwargs)
    return wrapped


def _ext_parse_scope(scope_raw: str) -> Tuple[str, date, date]:
    scope = (scope_raw or "today").strip().lower()
    today_d = _today_madrid()

    if scope == "today":
        return scope, today_d, today_d

    if scope == "week":
        monday_d = today_d - timedelta(days=today_d.weekday())
        sunday_d = monday_d + timedelta(days=6)
        return scope, monday_d, sunday_d

    if scope in ("next7", "7days"):
        end_d = today_d + timedelta(days=6)
        return "next7", today_d, end_d

    raise ValueError("Scope inválido")


def _to_hhmm(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "strftime"):
        return value.strftime("%H:%M")
    txt = str(value).strip()
    return txt[:5] if len(txt) >= 5 else None


@app.route("/api/extension/tasks")
@require_api_token
def api_extension_tasks():
    scope_raw = (request.args.get("scope") or "today").strip().lower()

    # Vista NextAction: tareas con etiqueta @NextAction (sin filtro de fecha)
    if scope_raw == "next":
        next_rows = q(
            "SELECT t.id, t.title, t.due_date, t.due_time, t.priority, t.completed_at, "
            "p.id AS project_id, p.name AS project_name, "
            "fd.id AS folder_id, fd.name AS folder_name "
            "FROM tasks t "
            "JOIN task_tags tt ON tt.task_id = t.id "
            "JOIN tags tg ON tg.id = tt.tag_id "
            "LEFT JOIN projects p ON p.id = t.project_id "
            "LEFT JOIN folders fd ON fd.id = COALESCE(t.folder_id, p.folder_id) "
            "WHERE LOWER(tg.name) = %s "
            "AND t.completed_at IS NULL "
            "AND t.archived = 0 "
            "AND t.deleted_at IS NULL "
            "AND (t.project_id IS NULL OR p.archived = 0) "
            "ORDER BY (t.priority IS NULL) ASC, t.priority ASC, "
            "(t.due_date IS NULL) ASC, t.due_date ASC, t.id DESC",
            (role_tag("next_action").lower(),),
        )
        next_items = []
        for r in next_rows:
            next_items.append({
                "id": int(r["id"]),
                "title": r.get("title") or "",
                "priority": int(r["priority"]) if r.get("priority") in (1, 2, 3) else None,
                "due_date": r["due_date"].isoformat() if r.get("due_date") else None,
                "due_time": _to_hhmm(r.get("due_time")),
                "project_id": int(r["project_id"]) if r.get("project_id") else None,
                "project_name": r.get("project_name") or None,
                "folder_id": int(r["folder_id"]) if r.get("folder_id") else None,
                "folder_name": r.get("folder_name") or None,
                "completed": bool(r.get("completed_at")),
            })
        return jsonify({"ok": True, "scope": "next", "from": None, "to": None, "items": next_items})

    try:
        scope, start_d, end_d = _ext_parse_scope(scope_raw)
    except ValueError:
        return jsonify({"ok": False, "error": "scope inválido. Usa today, week, next7 o next"}), 400

    rows = q(
        "SELECT t.id, t.title, t.due_date, t.due_time, t.priority, t.completed_at, "
        "p.id AS project_id, p.name AS project_name, "
        "fd.id AS folder_id, fd.name AS folder_name "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id = COALESCE(t.folder_id, p.folder_id) "
        "WHERE t.completed_at IS NULL "
        "AND t.archived=0 "
        "AND t.deleted_at IS NULL "
        "AND t.due_date IS NOT NULL "
        "AND t.due_date >= %s "
        "AND t.due_date <= %s "
        "AND (t.project_id IS NULL OR p.archived = 0) "
        "ORDER BY t.due_date ASC, (t.due_time IS NULL) ASC, t.due_time ASC, t.id DESC",
        (start_d, end_d),
    )

    items = []
    for r in rows:
        items.append(
            {
                "id": int(r["id"]),
                "title": r.get("title") or "",
                "priority": int(r["priority"]) if r.get("priority") in (1, 2, 3) else None,
                "due_date": r["due_date"].isoformat() if r.get("due_date") else None,
                "due_time": _to_hhmm(r.get("due_time")),
                "project_id": int(r["project_id"]) if r.get("project_id") else None,
                "project_name": r.get("project_name") or None,
                "folder_id": int(r["folder_id"]) if r.get("folder_id") else None,
                "folder_name": r.get("folder_name") or None,
                "completed": bool(r.get("completed_at")),
            }
        )

    return jsonify(
        {
            "ok": True,
            "scope": scope,
            "from": start_d.isoformat(),
            "to": end_d.isoformat(),
            "items": items,
        }
    )


@app.route("/api/extension/tasks/<int:task_id>/toggle", methods=["POST"])
@require_api_token
def api_extension_task_toggle(task_id: int):
    task = q1(
        "SELECT t.id, t.completed_at, t.due_date, t.recurrence_rule, t.max_occurrences, t.project_id, t.archived, "
        "rr.last_next_due_date "
        "FROM tasks t "
        "LEFT JOIN ("
        "SELECT task_id, MAX(next_due_date) AS last_next_due_date "
        "FROM recurring_task_runs "
        "GROUP BY task_id"
        ") rr ON rr.task_id=t.id "
        "WHERE t.id=%s",
        (task_id,),
    )
    if not task:
        return jsonify({"ok": False, "error": "Tarea no encontrada"}), 404

    now = datetime.now(ZoneInfo("Europe/Madrid")).replace(tzinfo=None)

    try:
        if task["completed_at"]:
            exec_sql("UPDATE tasks SET completed_at=NULL WHERE id=%s", (task_id,))
            _project_board_status_for_task(task_id, task.get("project_id"), "brainstorm")
            completed = False
        else:
            if task.get("recurrence_rule") and task.get("due_date"):
                today_d = now.date()
                recurrence_due_choice = (request.json or {}).get("recurrence_due_choice") if request.is_json else None
                complete_recurring_task_run(
                    task_id,
                    task,
                    now,
                    str(recurrence_due_choice) if recurrence_due_choice is not None else None,
                )
                completed = False
            else:
                complete_nonrecurring_task(task_id, task, now)
                completed = True

        _mark_task_calendar_dirty(task_id)
        commit()
        return jsonify({"ok": True, "task_id": task_id, "completed": completed})
    except Exception as e:
        rollback()
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/extension/tasks/add_page", methods=["POST"])
@require_api_token
def api_extension_add_page_task():
    payload = request.get_json(silent=True) or {}
    page_url = (payload.get("url") or "").strip()
    page_title = (payload.get("page_title") or "").strip()
    replacement_text = normalize_name(payload.get("replacement_text") or "")
    tags = payload.get("tags") or []
    project_name = (payload.get("project_name") or "").strip()
    priority = coerce_priority(payload.get("priority"), default=None)

    parsed = urlparse(page_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return jsonify({"ok": False, "error": "URL inválida"}), 400

    # Si hay texto de reemplazo, se guarda como enlace markdown en el título.
    # Si no, el título es la URL.
    if replacement_text:
        task_title = f"[{replacement_text}]({page_url})"
        notes = None
    else:
        task_title = page_url
        notes = page_title or None

    try:
        # Resolver proyecto si viene en el payload
        project_id = None
        if project_name:
            existing_project_id = find_project_by_name_active(project_name)
            if existing_project_id is not None:
                project_id = existing_project_id
            else:
                # Crear proyecto si no existe
                project_id = exec_sql(
                    "INSERT INTO projects(name, archived) VALUES(%s,%s)",
                    (project_name, 0),
                )

        task_id = exec_sql(
            "INSERT INTO tasks(title, notes, project_id, folder_id, due_date, due_time, recurrence_rule, priority) "
            "VALUES(%s,%s,%s,NULL,NULL,NULL,NULL,%s)",
            (task_title, notes, project_id, priority),
        )

        # Añadir etiquetas
        for tag_name in tags:
            tag_name = normalize_name(tag_name)
            if tag_name:
                tag_id = get_or_create_tag(tag_name)
                exec_sql(
                    "INSERT IGNORE INTO task_tags(task_id, tag_id) VALUES(%s,%s)",
                    (task_id, tag_id),
                )

        _mark_task_calendar_dirty(task_id, force_push_if_empty=True)
        commit()
        return jsonify(
            {
                "ok": True,
                "task": {
                    "id": int(task_id),
                    "title": task_title,
                    "notes": notes,
                    "project_id": project_id,
                    "project_name": project_name if project_id else None,
                },
            }
        )
    except Exception as e:
        rollback()
        return jsonify({"ok": False, "error": str(e)}), 500
