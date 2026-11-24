"""Consultas compartidas por varias vistas: árbol de carpetas, etiquetas por tarea, subtareas, permisos admin."""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from flask import session

from subtasks import DB as SubDB, register_subtask_routes

from gtd.core import app
from gtd.db import commit, exec_sql, q, q1, rollback


subdb = SubDB(q=q, q1=q1, exec_sql=exec_sql, commit=commit, rollback=rollback)
register_subtask_routes(app, subdb)


def load_folder_tree(include_archived: bool = False) -> List[Dict[str, Any]]:
    folders = q("SELECT id, parent_id, name FROM folders ORDER BY name")
    projects = q(
        "SELECT id, folder_id, name, archived FROM projects "
        + ("" if include_archived else "WHERE archived=0 ")
        + "ORDER BY name"
    )

    folder_map: Dict[int, Dict[str, Any]] = {}
    roots: List[Dict[str, Any]] = []

    for f in folders:
        folder_map[int(f["id"])] = {"id": int(f["id"]), "name": f["name"], "children": [], "projects": []}

    for f in folders:
        fid = int(f["id"])
        pid = f["parent_id"]
        if pid is None:
            roots.append(folder_map[fid])
        else:
            parent = folder_map.get(int(pid))
            if parent:
                parent["children"].append(folder_map[fid])
            else:
                roots.append(folder_map[fid])

    orphan_projects: List[Dict[str, Any]] = []
    for p in projects:
        item = {"id": int(p["id"]), "name": p["name"], "archived": int(p["archived"]) == 1}
        if p["folder_id"] is None:
            orphan_projects.append(item)
        else:
            node = folder_map.get(int(p["folder_id"]))
            if node is not None:
                node["projects"].append(item)
            else:
                orphan_projects.append(item)

    if orphan_projects:
        roots.append({"id": None, "name": "Proyectos (sin carpeta)", "children": [], "projects": orphan_projects})

    return roots


def build_folder_breadcrumb(folder_id: Optional[int], include_self: bool = True) -> List[Dict[str, Any]]:
    """Devuelve la ruta de carpetas desde raíz hasta folder_id."""
    if not folder_id:
        return []

    rows = q("SELECT id, parent_id, name FROM folders")
    folder_map: Dict[int, Dict[str, Any]] = {}
    for r in rows:
        folder_map[int(r["id"])] = {
            "id": int(r["id"]),
            "parent_id": int(r["parent_id"]) if r.get("parent_id") is not None else None,
            "name": r["name"],
        }

    current_id = int(folder_id)
    visited: set[int] = set()
    chain: List[Dict[str, Any]] = []

    while current_id in folder_map and current_id not in visited:
        visited.add(current_id)
        node = folder_map[current_id]
        chain.append({"id": node["id"], "name": node["name"]})
        parent_id = node["parent_id"]
        if parent_id is None:
            break
        current_id = parent_id

    chain.reverse()
    if not include_self and chain:
        return chain[:-1]
    return chain


def load_tags_map(task_ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
    if not task_ids:
        return {}
    placeholders = ",".join(["%s"] * len(task_ids))
    rows = q(
        f"SELECT tt.task_id, tg.id AS tag_id, tg.name "
        f"FROM task_tags tt "
        f"JOIN tags tg ON tg.id=tt.tag_id "
        f"WHERE tt.task_id IN ({placeholders}) "
        f"ORDER BY tg.name",
        tuple(task_ids),
    )
    out: Dict[int, List[Dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(int(r["task_id"]), []).append({"id": int(r["tag_id"]), "name": r["name"]})
    return out


def periodic_occurrences_badge(task: Dict[str, Any]) -> Optional[str]:
    """Devuelve el texto 'x/n' (ejecuciones/máximo) para una tarea periódica con
    max_occurrences definido, o None si no aplica. Solo necesita `task['id']` y
    `task['recurrence_rule']`; consulta el resto directamente en BBDD."""
    task_id = task.get("id")
    if not task_id or not task.get("recurrence_rule"):
        return None
    row = q1(
        "SELECT t.max_occurrences AS max_occurrences, "
        "(SELECT COUNT(*) FROM recurring_task_runs WHERE task_id=t.id) AS executed "
        "FROM tasks t WHERE t.id=%s",
        (int(task_id),),
    )
    if not row or not row.get("max_occurrences"):
        return None
    return f"{int(row['executed'])}/{int(row['max_occurrences'])} veces"


def admin_required() -> bool:
    pwd = os.environ.get("GTD_ADMIN_PASSWORD", "")
    if not pwd:
        return False
    return session.get("is_admin") is True


def get_api_token_owner_user_id() -> int:
    user_id = session.get("user_id")
    try:
        return int(user_id) if user_id is not None else 1
    except (TypeError, ValueError):
        return 1


def next_project_task_sort_order(project_id: int) -> int:
    row = q1(
        "SELECT COALESCE(MAX(sort_order), 0) + 1 AS next_order "
        "FROM tasks WHERE project_id=%s AND deleted_at IS NULL",
        (project_id,),
    )
    return int((row or {}).get("next_order") or 1)
