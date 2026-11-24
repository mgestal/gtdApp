"""Rutas del Kanban global y API de paneles/columnas."""
from __future__ import annotations

from flask import jsonify, render_template, request

from gtd.utils import normalize_tag_key
from gtd.core import app
from gtd.db import commit, q, q1, rollback
from gtd.roles import get_folder_tree_ids, role_folder_names
from gtd.kanban_board import (
    _kanban_column_matches_task,
    _kanban_columns,
    _kanban_matching_columns_after_move,
    _kanban_panels,
    _kanban_subcard_definitions,
    _kanban_unique_key,
    _save_kanban_columns,
    _save_kanban_panels,
    _sync_task_kanban_status,
    _validate_kanban_column_payload,
    _validate_kanban_panel_name,
)
from gtd.recurrence import recurring_effective_due_sql
from gtd.common import load_tags_map


@app.route("/kanban")
def kanban():
    recurring_due_join, effective_due_expr = recurring_effective_due_sql("t")
    filter_project_id = request.args.get("project_id", type=int)
    filter_folder_id = request.args.get("folder_id", type=int)
    filter_folder_recursive = (request.args.get("folder_recursive") or "").strip() in ("1", "true", "yes")
    filter_folder_ids = set()
    if filter_folder_id:
        filter_folder_ids = get_folder_tree_ids(filter_folder_id) if filter_folder_recursive else {filter_folder_id}

    active_rows = q(
        f"SELECT t.id, t.title, t.location, t.notes, {effective_due_expr} AS due_date, t.due_time, t.completed_at, t.priority, "
        "t.project_id, t.folder_id, COALESCE(t.folder_id, p.folder_id) AS context_folder_id, "
        "p.name AS project_name, "
        "fd.name AS folder_name "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        f"{recurring_due_join} "
        "LEFT JOIN folders fd ON fd.id = COALESCE(t.folder_id, p.folder_id) "
        "WHERE t.deleted_at IS NULL "
        "AND t.archived = 0 "
        "AND t.completed_at IS NULL "
        "AND (t.project_id IS NULL OR (p.archived = 0 AND p.deleted_at IS NULL)) "
        "ORDER BY t.id DESC",
    ) or []

    done_rows = q(
        f"SELECT t.id, t.title, t.location, t.notes, {effective_due_expr} AS due_date, t.due_time, t.completed_at, t.priority, "
        "t.project_id, t.folder_id, COALESCE(t.folder_id, p.folder_id) AS context_folder_id, "
        "p.name AS project_name, "
        "fd.name AS folder_name "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        f"{recurring_due_join} "
        "LEFT JOIN folders fd ON fd.id = COALESCE(t.folder_id, p.folder_id) "
        "WHERE t.deleted_at IS NULL "
        "AND t.archived = 0 "
        "AND t.completed_at IS NOT NULL "
        "AND (t.project_id IS NULL OR (p.archived = 0 AND p.deleted_at IS NULL)) "
        "ORDER BY t.completed_at DESC, t.id DESC",
    ) or []

    if filter_project_id:
        active_rows = [task for task in active_rows if task.get("project_id") == filter_project_id]
    if filter_folder_ids:
        active_rows = [task for task in active_rows if task.get("context_folder_id") in filter_folder_ids]

    kanban_projects = q(
        "SELECT id, name FROM projects WHERE archived=0 AND deleted_at IS NULL ORDER BY name"
    ) or []
    kanban_folders = q("SELECT id, parent_id, name FROM folders ORDER BY name") or []

    kanban_columns = _kanban_columns()
    tasks_by_status = {"inbox": []}
    for col in kanban_columns:
        tasks_by_status[col["key"]] = []

    folder_group_definitions = [
        {**definition, "folder_ids": set()}
        for definition in _kanban_subcard_definitions()
    ]
    kanban_folder_groups = {
        col["key"]: [
            {"key": group["key"], "label": group["label"], "tasks": [], "folder_ids": set()}
            for group in folder_group_definitions
        ]
        for col in kanban_columns
    }
    task_tags_by_id = load_tags_map([int(task["id"]) for task in active_rows])

    for group in folder_group_definitions:
        folder_name = (group.get("folder_name") or "").replace("@", "")
        folder_names = [folder_name]
        if folder_name in role_folder_names("agenda"):
            folder_names = [folder_name] + [n for n in role_folder_names("agenda") if n != folder_name]
        root_folder = q1(
            "SELECT id FROM folders WHERE LOWER(REPLACE(name, '@', '')) IN "
            "(" + ",".join(["LOWER(%s)"] * len(folder_names)) + ") "
            "ORDER BY (name=%s) DESC LIMIT 1",
            tuple(folder_names + [folder_name]),
        ) if folder_name else None
        if root_folder and root_folder.get("id") is not None:
            group["folder_ids"] = get_folder_tree_ids(int(root_folder["id"]))
            for status_groups in kanban_folder_groups.values():
                matching_group = next(item for item in status_groups if item["key"] == group["key"])
                matching_group["folder_ids"] = group["folder_ids"]

    for task in active_rows:
        context_folder_id = task.get("context_folder_id")
        task_tag_names = {
            normalize_tag_key(tag["name"])
            for tag in task_tags_by_id.get(int(task["id"]), [])
        }
        folder_group_definition = next(
            (
                group for group in folder_group_definitions
                if (
                    context_folder_id is not None and context_folder_id in group["folder_ids"]
                ) or (
                    group.get("tag_key") and group["tag_key"] in task_tag_names
                )
            ),
            None,
        )
        matching_columns = [
            col for col in kanban_columns
            if _kanban_column_matches_task(col, task, task_tag_names)
        ]

        # Cada columna es una lista independiente: una tarea puede cumplir
        # varios criterios y aparecer en todas las cards correspondientes.
        for matching_column in matching_columns:
            matching_status = matching_column["key"]
            if folder_group_definition and matching_status in kanban_folder_groups:
                matching_group = next(
                    group for group in kanban_folder_groups[matching_status]
                    if group["key"] == folder_group_definition["key"]
                )
                matching_group["tasks"].append(task)
            else:
                tasks_by_status[matching_status].append(task)

        # Inbox es además una vista propia de tareas sin proyecto/carpeta,
        # aunque alguna de ellas también coincida con una card etiquetada.
        is_real_inbox = task.get("project_id") is None and task.get("folder_id") is None
        if is_real_inbox:
            tasks_by_status["inbox"].append(task)

    kanban_counts = {
        status: len(tasks)
        for status, tasks in tasks_by_status.items()
    }
    for status, groups in kanban_folder_groups.items():
        kanban_counts[status] += sum(len(group["tasks"]) for group in groups)

    return render_template(
        "kanban.html",
        tasks_by_status=tasks_by_status,
        kanban_columns=kanban_columns,
        kanban_folder_groups=kanban_folder_groups,
        kanban_counts=kanban_counts,
        kanban_panels=_kanban_panels()[0],
        active_kanban_panel=_kanban_panels()[1],
        kanban_projects=kanban_projects,
        kanban_folders=kanban_folders,
        kanban_filter={
            "q": request.args.get("q", ""),
            "status": request.args.get("status", "all"),
            "project_id": filter_project_id,
            "folder_id": filter_folder_id,
            "folder_recursive": filter_folder_recursive,
        },
    )


@app.route("/api/tasks/<int:task_id>/kanban_status", methods=["POST"])
def api_task_kanban_status(task_id: int):
    payload = request.get_json(silent=True) or {}
    new_status = (payload.get("status") or "").strip().lower()
    source_status = (payload.get("source_status") or "").strip().lower() or None
    valid_statuses = {"inbox", "done"} | {col["key"] for col in _kanban_columns()}

    if new_status not in valid_statuses:
        return jsonify({"ok": False, "error": "Estado Kanban inválido"}), 400

    task = q1(
        "SELECT id, priority, project_id, folder_id FROM tasks WHERE id=%s AND deleted_at IS NULL",
        (task_id,),
    )
    if not task:
        return jsonify({"ok": False, "error": "Tarea no encontrada"}), 404

    try:
        matching_columns = _kanban_matching_columns_after_move(task, new_status, source_status)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        rollback()
        return jsonify({"ok": False, "error": str(exc)}), 500
    try:
        _sync_task_kanban_status(task_id, new_status, source_status)
        commit()
        return jsonify({"ok": True, "status": new_status})
    except Exception as exc:
        rollback()
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/kanban/panels", methods=["POST"])
def api_kanban_panel_create():
    payload = request.get_json(silent=True) or {}
    name, error = _validate_kanban_panel_name(payload)
    if error:
        return jsonify({"ok": False, "error": error}), 400

    panels, active_panel = _kanban_panels()
    panel_key = _kanban_unique_key(name, set(panels))
    columns = [] if payload.get("empty") else _kanban_columns()
    panels[panel_key] = {"name": name, "columns": columns}
    _save_kanban_panels(panels, panel_key)
    return jsonify({"ok": True, "key": panel_key, "name": name})


@app.route("/api/kanban/panels/<key>", methods=["POST"])
def api_kanban_panel_rename(key: str):
    payload = request.get_json(silent=True) or {}
    name, error = _validate_kanban_panel_name(payload)
    if error:
        return jsonify({"ok": False, "error": error}), 400

    panels, active_panel = _kanban_panels()
    panel = panels.get(key)
    if not panel:
        return jsonify({"ok": False, "error": "Panel no encontrado"}), 404

    panel["name"] = name
    _save_kanban_panels(panels, active_panel)
    return jsonify({"ok": True, "key": key, "name": name})


@app.route("/api/kanban/panels/<key>/activate", methods=["POST"])
def api_kanban_panel_activate(key: str):
    panels, active_panel = _kanban_panels()
    if key not in panels:
        return jsonify({"ok": False, "error": "Panel no encontrado"}), 404

    _save_kanban_panels(panels, key)
    return jsonify({"ok": True, "key": key})


@app.route("/api/kanban/panels/<key>", methods=["DELETE"])
def api_kanban_panel_delete(key: str):
    panels, active_panel = _kanban_panels()
    if key not in panels:
        return jsonify({"ok": False, "error": "Panel no encontrado"}), 404
    if len(panels) == 1:
        return jsonify({"ok": False, "error": "Debe existir al menos un panel"}), 400

    del panels[key]
    next_active_panel = next(iter(panels)) if active_panel == key else active_panel
    _save_kanban_panels(panels, next_active_panel)
    return jsonify({"ok": True, "key": next_active_panel})


@app.route("/api/kanban/columns", methods=["POST"])
def api_kanban_column_create():
    payload = request.get_json(silent=True) or {}
    label, tag_name, expression, priority_filter, width, error = _validate_kanban_column_payload(payload)
    if error:
        return jsonify({"ok": False, "error": error}), 400

    columns = _kanban_columns()
    existing_keys = {c["key"] for c in columns} | {"inbox", "done"}
    new_key = _kanban_unique_key(label, existing_keys)
    columns.append({"key": new_key, "label": label, "tag_name": tag_name, "expression": expression, "priority_filter": priority_filter, "width": width})
    _save_kanban_columns(columns)
    return jsonify({"ok": True, "column": {"key": new_key, "label": label, "tag_name": tag_name, "expression": expression, "priority_filter": priority_filter, "width": width}})


@app.route("/api/kanban/columns/reorder", methods=["POST"])
def api_kanban_columns_reorder():
    payload = request.get_json(silent=True) or {}
    ordered_keys = payload.get("keys") or []
    columns = _kanban_columns()
    if set(ordered_keys) != {column["key"] for column in columns}:
        return jsonify({"ok": False, "error": "Orden de cards inválido"}), 400

    ordered_columns = [next(column for column in columns if column["key"] == key) for key in ordered_keys]
    _save_kanban_columns(ordered_columns)
    return jsonify({"ok": True})


@app.route("/api/kanban/columns/<key>", methods=["POST"])
def api_kanban_column_update(key: str):
    if key in ("inbox", "done"):
        return jsonify({"ok": False, "error": "Esa columna no se puede editar"}), 400

    columns = _kanban_columns()
    if not any(c["key"] == key for c in columns):
        return jsonify({"ok": False, "error": "Columna no encontrada"}), 404

    payload = request.get_json(silent=True) or {}
    label, tag_name, expression, priority_filter, width, error = _validate_kanban_column_payload(payload)
    if error:
        return jsonify({"ok": False, "error": error}), 400

    for c in columns:
        if c["key"] == key:
            c["label"] = label
            c["tag_name"] = tag_name
            c["expression"] = expression
            c["priority_filter"] = priority_filter
            c["width"] = width
            break
    _save_kanban_columns(columns)
    return jsonify({"ok": True, "column": {"key": key, "label": label, "tag_name": tag_name, "expression": expression, "priority_filter": priority_filter, "width": width}})


@app.route("/api/kanban/columns/<key>", methods=["DELETE"])
def api_kanban_column_delete(key: str):
    if key in ("inbox", "done"):
        return jsonify({"ok": False, "error": "Esa columna no se puede eliminar"}), 400

    columns = _kanban_columns()
    remaining = [c for c in columns if c["key"] != key]
    if len(remaining) == len(columns):
        return jsonify({"ok": False, "error": "Columna no encontrada"}), 404

    _save_kanban_columns(remaining)
    return jsonify({"ok": True})
