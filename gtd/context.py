"""Context processors de plantillas (contadores y barra lateral)."""
from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any, Dict, List, Optional

from flask import request, session, url_for
from markupsafe import Markup

from gtd.config import load_config
from gtd.utils import _today_madrid, location_to_href
from gtd.core import app
from gtd.db import q, q1
from gtd.roles import gtd_roles, role_tag
from gtd.filter_expr import (
    And,
    ast_contains_done,
    compile_filter_to_sql,
    Not,
    parse_filter_expression,
    Term,
)
from gtd.recurrence import recurring_effective_due_sql
from gtd.common import load_folder_tree, periodic_occurrences_badge


@app.context_processor
def inject_common():
    cfg = load_config()
    session.setdefault("notification_csrf", secrets.token_urlsafe(32))
    reminder_task_ids = {row["task_id"] for row in q("SELECT DISTINCT task_id FROM task_reminders")}

    def reminder_badge(task_id):
        if task_id not in reminder_task_ids:
            return Markup("")
        href = url_for("task_edit", task_id=task_id, next=request.full_path) + "#recordatorios"
        return Markup(' <a href="{}" class="reminder-badge" title="Ver recordatorios" '
                      'aria-label="Ver recordatorios">🔔</a>').format(href)

    return {
        "APP_TITLE": cfg.get("app", {}).get("title", "GTD App"),
        "folder_tree": load_folder_tree(include_archived=False),
        "all_folders": q("SELECT id, name FROM folders ORDER BY name"),
        "all_projects": q("SELECT id, name FROM projects WHERE archived=0 AND deleted_at IS NULL ORDER BY name"),
        "TAG_SEARCH_URL": url_for("api_tags_search"),
        "PROJECT_SEARCH_URL": url_for("api_projects_search"),
        "FOLDER_SEARCH_URL": url_for("api_folders_search"),
        "SCRIPT_ROOT": request.script_root or "",
        "location_to_href": location_to_href,
        "periodic_occurrences_badge": periodic_occurrences_badge,
        "reminder_badge": reminder_badge,
        "notification_csrf": session["notification_csrf"],
        # Nombres canónicos de los roles GTD (ver gtd_roles.py), p.ej. {{ R_TAG.next_action }}
        "R_TAG": {role: names[0] for role, names in gtd_roles()["tags"].items()},
        "R_FOLDER": {role: names[0] for role, names in gtd_roles()["folders"].items()},
    }

@app.context_processor
def inject_sidebar_counts():
    try:
        today_d = _today_madrid()
        sunday_d = today_d + timedelta(days=(6 - today_d.weekday()))
        recurring_due_join, effective_due_expr = recurring_effective_due_sql("t")
        
        
        next_tag = q1("SELECT id FROM tags WHERE name=%s", (role_tag("next_action"),))

        if next_tag:
            next_count = int((q1(
                "SELECT COUNT(*) AS c "
                "FROM tasks t "
                "JOIN task_tags tt ON tt.task_id=t.id "
                "LEFT JOIN projects p ON p.id=t.project_id "
                "WHERE t.completed_at IS NULL AND t.archived=0 AND t.deleted_at IS NULL "
                "AND tt.tag_id=%s "
                "AND (t.project_id IS NULL OR p.archived = 0)",
                (next_tag["id"],)
            ) or {}).get("c", 0))
        else:
            next_count = -1

        counts = {
            "next": next_count,
            
            "inbox": int((q1(
                "SELECT COUNT(*) AS c "
                "FROM tasks "
                "WHERE completed_at IS NULL AND archived=0 AND deleted_at IS NULL "
                "AND project_id IS NULL AND folder_id IS NULL"
            ) or {}).get("c", 0)),

            "today": int((q1(
                f"SELECT COUNT(*) AS c "
                "FROM tasks t "
                "LEFT JOIN projects p ON p.id=t.project_id "
                f"{recurring_due_join}"
                f"WHERE t.completed_at IS NULL AND t.archived=0 AND t.deleted_at IS NULL AND {effective_due_expr}=%s "
                "AND (t.project_id IS NULL OR p.archived = 0)",
                (today_d,)
            ) or {}).get("c", 0)),

            "week": int((q1(
                f"SELECT COUNT(*) AS c "
                "FROM tasks t "
                "LEFT JOIN projects p ON p.id=t.project_id "
                f"{recurring_due_join}"
                "WHERE t.completed_at IS NULL AND t.archived=0 AND t.deleted_at IS NULL "
                f"AND {effective_due_expr} IS NOT NULL "
                f"AND {effective_due_expr} >= %s "
                f"AND {effective_due_expr} <= %s "
                "AND (t.project_id IS NULL OR p.archived = 0)",
                (today_d, sunday_d)
            ) or {}).get("c", 0)),

            "next": next_count,

            "agenda": int((q1(
                f"SELECT COUNT(*) AS c "
                "FROM tasks t "
                "LEFT JOIN projects p ON p.id=t.project_id "
                f"{recurring_due_join}"
                f"WHERE t.completed_at IS NULL AND t.archived=0 AND t.deleted_at IS NULL AND {effective_due_expr} IS NOT NULL "
                "AND (t.project_id IS NULL OR p.archived = 0)"
            ) or {}).get("c", 0)),

            "projects": int((q1(
                "SELECT COUNT(*) AS c "
                "FROM projects "
                "WHERE archived=0"
            ) or {}).get("c", 0)),

            "tags": int((q1(
                "SELECT COUNT(*) AS c "
                "FROM tags"
            ) or {}).get("c", 0)),

            "filters": int((q1(
                "SELECT COUNT(*) AS c "
                "FROM filters"
            ) or {}).get("c", 0)),
        }

    except Exception:
        counts = {
            "next": 0,
            "inbox": 0,
            "today": 0,
            "week": 0,
            "agenda": 0,
            "projects": 0,
            "tags": 0,
            "filters": 0,
        }

    return {"sidebar_counts": counts}


def _sidebar_filter_count(expression: str) -> Optional[int]:
    try:
        recurring_due_join, effective_due_expr = recurring_effective_due_sql("t")
        ast = parse_filter_expression(expression)
        if not ast_contains_done(ast):
            ast = And(ast, Not(Term("IDENT", "done")))
        where_sql, params = compile_filter_to_sql(ast)
        where_sql = where_sql.replace("t.due_date", effective_due_expr)
        row = q1(
            "SELECT COUNT(*) AS c "
            "FROM tasks t "
            "LEFT JOIN projects p ON p.id=t.project_id "
            "LEFT JOIN folders fd ON fd.id=t.folder_id "
            "LEFT JOIN folders pf ON pf.id=p.folder_id "
            f"{recurring_due_join}"
            f"WHERE {where_sql} AND t.archived=0 AND t.deleted_at IS NULL AND (t.project_id IS NULL OR p.archived = 0)",
            tuple(params),
        )
        return int(row["c"]) if row else 0
    except Exception:
        return None


def _sidebar_filters_ordered() -> List[Dict[str, Any]]:
    rows = q("SELECT id, name, expression FROM filters ORDER BY name") or []
    saved_order = ((load_config().get("sidebar") or {}).get("filter_order")) or []
    position = {fid: i for i, fid in enumerate(saved_order) if isinstance(fid, int)}
    rows.sort(key=lambda r: (position.get(int(r["id"]), len(position)), (r["name"] or "").lower()))
    return rows


def _sidebar_tags_ordered() -> List[Dict[str, Any]]:
    tag_sidebar_cfg = (load_config().get("sidebar") or {}).get("tag_task_visibility") or {}
    include_completed = bool(tag_sidebar_cfg.get("completed", False))
    include_archived = bool(tag_sidebar_cfg.get("archived", False))
    completed_clause = "" if include_completed else "AND t.completed_at IS NULL "
    archived_clause = "" if include_archived else "AND t.archived=0 "
    project_archived_clause = "" if include_archived else "AND (t.project_id IS NULL OR p.archived=0) "
    rows = q(
        "SELECT tg.id, tg.name, ttype.name AS type, MAX(loc.tag_id IS NOT NULL) AS has_location, "
        "COUNT(CASE WHEN t.id IS NOT NULL "
        f"{completed_clause}{archived_clause}{project_archived_clause}THEN 1 END) AS task_count "
        "FROM tags tg LEFT JOIN TagsType ttype ON ttype.id=tg.type_id "
        "LEFT JOIN tag_locations loc ON loc.tag_id=tg.id "
        "LEFT JOIN task_tags tasktag ON tasktag.tag_id=tg.id "
        "LEFT JOIN tasks t ON t.id=tasktag.task_id AND t.deleted_at IS NULL "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "GROUP BY tg.id, tg.name, ttype.name "
        "ORDER BY tg.name"
    ) or []
    saved_order = ((load_config().get("sidebar") or {}).get("tag_order")) or []
    position = {tag_id: i for i, tag_id in enumerate(saved_order) if isinstance(tag_id, int)}
    rows.sort(key=lambda r: (position.get(int(r["id"]), len(position)), (r["name"] or "").lower()))
    return rows


@app.context_processor
def inject_sidebar_filters():
    try:
        current_filter_id = None
        if request.endpoint == "filter_run" and request.view_args:
            current_filter_id = request.view_args.get("filter_id")
        filters = [
            {"id": int(r["id"]), "name": r["name"], "count": _sidebar_filter_count(r["expression"])}
            for r in _sidebar_filters_ordered()
        ]
        current_tag_id = None
        tag_sidebar_cfg = (load_config().get("sidebar") or {}).get("tag_task_visibility") or {}
        if request.endpoint == "tag_detail" and request.view_args:
            current_tag_id = request.view_args.get("tag_id")
        groups_by_type: Dict[str, Dict[str, Any]] = {}
        for r in _sidebar_tags_ordered():
            tag_type = (r.get("type") or "").strip()
            group = groups_by_type.setdefault(tag_type.lower(), {"type": tag_type, "tags": []})
            group["tags"].append({"id": int(r["id"]), "name": r["name"], "count": int(r["task_count"] or 0),
                                  "has_location": bool(r["has_location"])})
        tag_groups = sorted(
            (g for k, g in groups_by_type.items() if k),
            key=lambda g: g["type"].lower(),
        )
        tag_groups.append(groups_by_type.get("", {"type": "", "tags": []}))
        return {
            "sidebar_filters": filters,
            "current_filter_id": current_filter_id,
            "sidebar_tag_groups": tag_groups,
            "current_tag_id": current_tag_id,
            "tag_count_completed": bool(tag_sidebar_cfg.get("completed", False)),
            "tag_count_archived": bool(tag_sidebar_cfg.get("archived", False)),
        }
    except Exception:
        return {
            "sidebar_filters": [],
            "current_filter_id": None,
            "sidebar_tag_groups": [],
            "current_tag_id": None,
            "tag_count_completed": False,
            "tag_count_archived": False,
        }


@app.context_processor
def inject_sidebar_tree():
    try:
        # 1) Carpetas
        folders = q("SELECT id, parent_id, name FROM folders ORDER BY name")
        folders_by_parent = {}
        parent_of = {}
        children_of = {}

        for f in folders:
            pid = f.get("parent_id")
            folders_by_parent.setdefault(pid, []).append(f)
            parent_of[f["id"]] = pid
            children_of.setdefault(pid, []).append(f["id"])
            children_of.setdefault(f["id"], [])

        # 2) Proyectos activos
        projects = q(
            "SELECT id, name, folder_id "
            "FROM projects "
            "WHERE archived=0 "
            "ORDER BY name"
        )

        # Contador de tareas abiertas por proyecto
        rows = q(
            "SELECT project_id, COUNT(*) AS n "
            "FROM tasks "
            "WHERE completed_at IS NULL "
            "AND archived=0 "
            "AND deleted_at IS NULL "
            "AND project_id IS NOT NULL "
            "GROUP BY project_id"
        )
        project_task_counts = {int(r["project_id"]): int(r["n"]) for r in rows}

        projects_by_folder = {}
        for p in projects:
            fid = p.get("folder_id")
            if fid is None:
                continue
            projects_by_folder.setdefault(fid, []).append(p)

        # --- NUEVO: contador directo de tareas por carpeta ---
        # solo tareas asignadas directamente a carpeta (no a proyecto)
        folder_direct_rows = q(
            "SELECT folder_id, COUNT(*) AS n "
            "FROM tasks "
            "WHERE completed_at IS NULL "
            "AND archived=0 "
            "AND deleted_at IS NULL "
            "AND folder_id IS NOT NULL "
            "AND project_id IS NULL "
            "GROUP BY folder_id"
        )
        folder_direct_counts = {int(r["folder_id"]): int(r["n"]) for r in folder_direct_rows}

        # 3) Carpeta y proyecto actual
        current_folder_id = None
        current_project_id = None
        ep = request.endpoint or ""

        if ep == "folder_detail":
            fid = request.view_args.get("folder_id") if request.view_args else None
            try:
                current_folder_id = int(fid) if fid is not None else None
            except (TypeError, ValueError):
                current_folder_id = None

        elif ep == "project_detail":
            pid = request.view_args.get("project_id") if request.view_args else None
            try:
                pid_int = int(pid) if pid is not None else None
            except (TypeError, ValueError):
                pid_int = None

            if pid_int is not None:
                current_project_id = pid_int
                row = q1("SELECT folder_id FROM projects WHERE id=%s", (pid_int,))
                if row and row.get("folder_id") is not None:
                    try:
                        current_folder_id = int(row["folder_id"])
                    except (TypeError, ValueError):
                        current_folder_id = None

        open_folder_ids = set()
        if current_folder_id is not None:
            cur = current_folder_id
            guard = 0
            while cur is not None and guard < 100:
                open_folder_ids.add(cur)
                cur = parent_of.get(cur)
                guard += 1

        return {
            "folders_by_parent": folders_by_parent,
            "projects_by_folder": projects_by_folder,
            "project_task_counts": project_task_counts,
            "folder_task_counts": folder_direct_counts,   # <- nuevo
            "current_folder_id": current_folder_id,
            "current_project_id": current_project_id,
            "open_folder_ids": open_folder_ids,
        }

    except Exception:
        return {
            "folders_by_parent": {},
            "projects_by_folder": {},
            "project_task_counts": {},
            "folder_task_counts": {},   # <- nuevo
            "current_folder_id": None,
            "current_project_id": None,
            "open_folder_ids": set(),
        }
