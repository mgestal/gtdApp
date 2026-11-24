"""Lógica del Kanban global y de los tableros por proyecto (sin rutas)."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from flask import g

from gtd.config import DEFAULT_KANBAN_SUBCARDS, load_config, save_config
from gtd.utils import normalize_name, normalize_tag_key
from gtd.db import exec_sql, q, q1
from gtd.lookups import get_or_create_tag
from gtd.roles import role_folder_names
from gtd.filter_expr import And, FilterParseError, Node, Not, Or, parse_filter_expression, Term


DEFAULT_KANBAN_COLUMNS = [
    {"key": "next", "label": "Próximo", "tag_name": "NextAction"},
    {"key": "doing", "label": "En proceso", "tag_name": "enCurso"},
    {"key": "waiting", "label": "Esperando", "tag_name": "EnEspera"},
]


def _kanban_panels():
    cfg = getattr(g, "cfg", None) or load_config()
    kanban_cfg = cfg.get("kanban") or {}
    panels = kanban_cfg.get("panels")
    if panels:
        return panels, kanban_cfg.get("active_panel") or next(iter(panels))

    columns = kanban_cfg.get("columns")
    if not columns:
        legacy_tags = kanban_cfg.get("status_tags") or {}
        columns = [
            {
                "key": col["key"],
                "label": col["label"],
                "tag_name": normalize_name(legacy_tags.get(col["key"], col["tag_name"])).lstrip("@") or col["tag_name"],
            }
            for col in DEFAULT_KANBAN_COLUMNS
        ]
    return {"principal": {"name": "Principal", "columns": columns}}, "principal"


def _kanban_columns():
    panels, active_panel = _kanban_panels()
    panel = panels.get(active_panel) or next(iter(panels.values()))
    columns = panel.get("columns") or []

    result = []
    for col in columns:
        key = str(col.get("key") or "").strip()
        tag_name = normalize_name(col.get("tag_name")).lstrip("@")
        expression = (col.get("expression") or "").strip()
        priority_filter = col.get("priority_filter")
        if priority_filter not in (1, 2, 3, "none"):
            priority_filter = None
        try:
            column_width = int(col.get("width", 1))
        except (TypeError, ValueError):
            column_width = 1
        if column_width not in (1, 2, 3):
            column_width = 1
        if not key or (not tag_name and not expression and priority_filter is None):
            continue
        result.append({
            "key": key,
            "label": normalize_name(col.get("label")) or key,
            "tag_name": tag_name,
            "expression": expression,
            "priority_filter": priority_filter,
            "width": column_width,
        })
    return result


def _parse_kanban_tag_expression(expression: str):
    parts = re.split(r"\s*(?:\^|&|\bAND\b)\s*", expression.strip(), flags=re.I)
    if not expression.strip() or any(not part for part in parts):
        raise ValueError("La expresión debe combinar etiquetas con AND")

    terms = []
    polarities = {}
    for part in parts:
        match = re.fullmatch(
            r"(?:(?:NOT)\s+|!)?@([A-Za-z0-9_\-áéíóúÁÉÍÓÚñÑ]+)",
            part,
            flags=re.I,
        )
        if not match:
            raise ValueError("Solo se permiten etiquetas y los operadores AND y NOT, sin paréntesis")
        tag_name = match.group(1)
        negated = bool(re.match(r"(?:(?:NOT)\s+|!)", part, flags=re.I))
        tag_key = normalize_tag_key(tag_name)
        if tag_key in polarities and polarities[tag_key] != negated:
            raise ValueError(f"La etiqueta @{tag_name} no puede aparecer con AND y NOT a la vez")
        polarities[tag_key] = negated
        terms.append((tag_name, negated))
    return terms


def _kanban_filter_parser_expression(expression: str) -> str:
    expression = expression.replace("^", "&")
    return re.sub(r"(?<![@\w])NOT\s+(?=@)", "!", expression, flags=re.I)


def _kanban_expression_matches(task_id: int, expression: str, tag_names=None) -> bool:
    try:
        if "(" in expression or ")" in expression:
            return False
        ast = parse_filter_expression(_kanban_filter_parser_expression(expression))
    except FilterParseError:
        return False

    if tag_names is None:
        tag_names = {
            normalize_tag_key(row["name"])
            for row in q(
                "SELECT tg.name FROM task_tags tt JOIN tags tg ON tg.id=tt.tag_id WHERE tt.task_id=%s",
                (task_id,),
            )
        }
    else:
        tag_names = {normalize_tag_key(name) for name in tag_names}

    def matches(node: Node) -> bool:
        if isinstance(node, Term):
            return node.kind == "TAG" and normalize_tag_key(node.value) in tag_names
        if isinstance(node, Not):
            return not matches(node.child)
        if isinstance(node, And):
            return matches(node.left) and matches(node.right)
        if isinstance(node, Or):
            return matches(node.left) or matches(node.right)
        return False

    return matches(ast)


def _kanban_column_matches_task(column: Dict[str, Any], task: Dict[str, Any], tag_names=None) -> bool:
    priority_filter = column.get("priority_filter")
    if priority_filter is not None:
        return task.get("priority") is None if priority_filter == "none" else task.get("priority") == priority_filter
    if column.get("expression"):
        return _kanban_expression_matches(task["id"], column["expression"], tag_names)
    if tag_names is not None:
        return normalize_tag_key(column.get("tag_name") or "") in {
            normalize_tag_key(name) for name in tag_names
        }
    return _task_has_tag(task["id"], column.get("tag_name") or "")


def _kanban_column_by_key(key: str):
    return next((c for c in _kanban_columns() if c["key"] == key), None)


def _kanban_unique_key(label: str, existing_keys) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", normalize_tag_key(label)).strip("-") or "columna"
    key = base
    i = 2
    while key in existing_keys:
        key = f"{base}-{i}"
        i += 1
    return key


def _save_kanban_panels(panels, active_panel: str) -> None:
    cfg = load_config()
    kanban_cfg = cfg.setdefault("kanban", {})
    kanban_cfg["panels"] = panels
    kanban_cfg["active_panel"] = active_panel
    kanban_cfg.pop("columns", None)
    kanban_cfg.pop("status_tags", None)
    save_config(cfg)
    g.cfg = cfg


def _save_kanban_columns(columns) -> None:
    panels, active_panel = _kanban_panels()
    panels[active_panel]["columns"] = columns
    _save_kanban_panels(panels, active_panel)


def _task_has_tag(task_id: int, tag_name: str) -> bool:
    row = q1(
        "SELECT 1 AS ok FROM task_tags tt JOIN tags tg ON tg.id=tt.tag_id WHERE tt.task_id=%s AND LOWER(tg.name)=LOWER(%s) LIMIT 1",
        (task_id, tag_name),
    )
    return bool(row)


def _remove_kanban_column_tags(task_id: int, column) -> None:
    """Remove the positive tags that identify a task's source column."""
    if not column or column.get("priority_filter") is not None:
        return
    if column.get("expression"):
        terms = _parse_kanban_tag_expression(column["expression"])
        tag_names = {name for name, negated in terms if not negated}
    else:
        tag_names = {column.get("tag_name")} if column.get("tag_name") else set()
    for tag_name in tag_names:
        _remove_task_tag(task_id, tag_name)


def _sync_task_kanban_status(task_id: int, new_status: str, source_status: str = None) -> None:
    columns = _kanban_columns()
    column = _kanban_column_by_key(new_status)
    source_column = _kanban_column_by_key(source_status) if source_status else None
    _remove_kanban_column_tags(task_id, source_column)

    if column and column.get("priority_filter") is not None:
        priority = None if column["priority_filter"] == "none" else column["priority_filter"]
        exec_sql("UPDATE tasks SET priority=%s WHERE id=%s", (priority, task_id))
        return

    if new_status == "inbox":
        _remove_kanban_state_tags(task_id, columns)
        exec_sql("UPDATE tasks SET completed_at = NULL WHERE id=%s", (task_id,))
        task = q1("SELECT project_id FROM tasks WHERE id=%s", (task_id,))
        _project_board_status_for_task(task_id, task.get("project_id") if task else None, "brainstorm")
        return

    if new_status == "done":
        _remove_kanban_state_tags(task_id, columns)
        exec_sql("UPDATE tasks SET completed_at = NOW() WHERE id=%s", (task_id,))
        task = q1("SELECT project_id FROM tasks WHERE id=%s", (task_id,))
        _project_board_status_for_task(task_id, task.get("project_id") if task else None, "done")
        return

    if not column:
        return

    if column.get("expression"):
        terms = _parse_kanban_tag_expression(column["expression"])
        for tag_name, _ in terms:
            _remove_task_tag(task_id, tag_name)
        for tag_name, negated in terms:
            if not negated:
                tag_id = get_or_create_tag(tag_name)
                exec_sql(
                    "INSERT IGNORE INTO task_tags(task_id, tag_id) VALUES(%s, %s)",
                    (task_id, tag_id),
                )
        return

    _remove_kanban_state_tags(task_id, columns)

    tag_id = get_or_create_tag(column["tag_name"])
    exec_sql(
        "INSERT IGNORE INTO task_tags(task_id, tag_id) VALUES(%s, %s)",
        (task_id, tag_id),
    )

    # Si la tarea ha dejado atrás un estado de GTD previa, lo dejamos sincronizado
    # con la etiqueta del nuevo estado del tablero.


def _remove_task_tag(task_id: int, tag_name: str) -> None:
    exec_sql(
        "DELETE FROM task_tags "
        "WHERE task_id=%s AND tag_id IN ("
        "SELECT id FROM (SELECT tg.id FROM tags tg WHERE LOWER(tg.name)=LOWER(%s)) AS tag_matches"
        ")",
        (task_id, tag_name),
    )


def _remove_kanban_state_tags(task_id: int, columns) -> None:
    for tag_name in {col["tag_name"] for col in columns if col.get("tag_name")}:
        _remove_task_tag(task_id, tag_name)


def _kanban_matching_columns_after_move(task: Dict[str, Any], new_status: str, source_status: str = None):
    columns = _kanban_columns()
    tags = {
        normalize_tag_key(row["name"])
        for row in q(
            "SELECT tg.name FROM task_tags tt JOIN tags tg ON tg.id=tt.tag_id WHERE tt.task_id=%s",
            (task["id"],),
        )
    }
    resulting_task = dict(task)
    column = next((col for col in columns if col["key"] == new_status), None)
    source_column = next((col for col in columns if col["key"] == source_status), None) if source_status else None
    if source_column:
        if source_column.get("expression"):
            source_terms = _parse_kanban_tag_expression(source_column["expression"])
            for tag_name, negated in source_terms:
                if not negated:
                    tags.discard(normalize_tag_key(tag_name))
        elif source_column.get("tag_name"):
            tags.discard(normalize_tag_key(source_column["tag_name"]))

    if new_status == "done":
        return []
    if new_status == "inbox":
        for state_tag in {col["tag_name"] for col in columns if col.get("tag_name")}:
            tags.discard(normalize_tag_key(state_tag))
    elif column and column.get("priority_filter") is not None:
        resulting_task["priority"] = None if column["priority_filter"] == "none" else column["priority_filter"]
    elif column and column.get("expression"):
        for tag_name, negated in _parse_kanban_tag_expression(column["expression"]):
            tag_key = normalize_tag_key(tag_name)
            tags.discard(tag_key)
            if not negated:
                tags.add(tag_key)
    elif column:
        for state_tag in {col["tag_name"] for col in columns if col.get("tag_name")}:
            tags.discard(normalize_tag_key(state_tag))
        tags.add(normalize_tag_key(column.get("tag_name") or ""))

    matching_columns = [
        col for col in columns
        if _kanban_column_matches_task(col, resulting_task, tags)
    ]
    if new_status == "inbox" and not matching_columns:
        if task.get("project_id") is None and task.get("folder_id") is None:
            return [{"key": "inbox", "label": "Inbox"}]
    return matching_columns


DEFAULT_PROJECT_BOARD_COLUMNS = [
    {"key": "brainstorm", "label": "Brainstorm"},
    {"key": "to-do", "label": "To-Do"},
    {"key": "in-progress", "label": "In-Progress"},
    {"key": "done", "label": "Done"},
]


def _project_board_columns(project: Dict[str, Any]) -> List[Dict[str, str]]:
    raw_columns = project.get("board_columns")
    if isinstance(raw_columns, str):
        try:
            raw_columns = json.loads(raw_columns)
        except (TypeError, ValueError):
            raw_columns = None

    columns = raw_columns if isinstance(raw_columns, list) else []
    result = []
    used_keys = set()
    for column in columns:
        if not isinstance(column, dict):
            continue
        key = str(column.get("key") or "").strip().lower()
        label = normalize_name(column.get("label"))
        if not key or not re.fullmatch(r"[a-z0-9-]{1,80}", key) or not label or key in used_keys:
            continue
        result.append({"key": key, "label": label})
        used_keys.add(key)

    if "done" not in used_keys:
        result.append({"key": "done", "label": "Done"})
    return result


def _project_board_is_initialized(project: Dict[str, Any]) -> bool:
    return bool(project.get("board_columns"))


def _initialize_project_board(project_id: int) -> List[Dict[str, str]]:
    project = q1("SELECT id, board_columns FROM projects WHERE id=%s", (project_id,))
    if not project:
        raise ValueError("Proyecto no encontrado")

    columns = _project_board_columns(project)
    if not _project_board_is_initialized(project):
        exec_sql(
            "UPDATE projects SET board_columns=%s, updated_at=NOW() WHERE id=%s",
            (json.dumps(DEFAULT_PROJECT_BOARD_COLUMNS), project_id),
        )
        columns = list(DEFAULT_PROJECT_BOARD_COLUMNS)

    exec_sql(
        "UPDATE tasks SET project_board_status='brainstorm', project_board_order=id "
        "WHERE project_id=%s AND completed_at IS NULL AND archived=0 AND deleted_at IS NULL "
        "AND project_board_status IS NULL",
        (project_id,),
    )
    exec_sql(
        "UPDATE tasks SET project_board_status='done', project_board_order=id "
        "WHERE project_id=%s AND completed_at IS NOT NULL AND archived=0 AND deleted_at IS NULL "
        "AND project_board_status IS NULL",
        (project_id,),
    )
    return columns


def _project_board_status_for_task(task_id: int, project_id: Optional[int], status: str) -> None:
    if not project_id:
        return
    project = q1("SELECT id, board_columns FROM projects WHERE id=%s", (project_id,))
    if not project or not _project_board_is_initialized(project):
        return
    valid_statuses = {column["key"] for column in _project_board_columns(project)}
    target_status = status if status in valid_statuses else "brainstorm"
    exec_sql(
        "UPDATE tasks SET project_board_status=%s, project_board_order=id WHERE id=%s",
        (target_status, task_id),
    )


def _save_project_board_columns(project_id: int, columns: List[Dict[str, str]]) -> None:
    normalized_columns = _project_board_columns({"board_columns": columns})
    exec_sql(
        "UPDATE projects SET board_columns=%s, updated_at=NOW() WHERE id=%s",
        (json.dumps(normalized_columns), project_id),
    )


def _project_board_column_key(label: str, columns: List[Dict[str, str]]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", normalize_tag_key(label)).strip("-") or "columna"
    key = base
    suffix = 2
    existing_keys = {column["key"] for column in columns}
    while key in existing_keys or key == "done":
        key = f"{base}-{suffix}"
        suffix += 1
    return key


def _kanban_subcard_definitions() -> List[Dict[str, str]]:
    raw = (load_config().get("kanban") or {}).get("subcards")
    if not isinstance(raw, list):
        raw = DEFAULT_KANBAN_SUBCARDS
    definitions: List[Dict[str, str]] = []
    used_keys: set = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        if not label:
            continue
        base_key = re.sub(r"[^a-z0-9]+", "-", normalize_tag_key(item.get("key") or label)).strip("-") or "subcard"
        key, suffix = base_key, 2
        while key in used_keys:
            key = f"{base_key}-{suffix}"
            suffix += 1
        used_keys.add(key)
        folder_name = str(item.get("folder_name") or "").strip()
        agenda_folder_names = role_folder_names("agenda")
        if folder_name in agenda_folder_names[1:]:
            folder_name = agenda_folder_names[0]
        definitions.append({
            "key": key,
            "label": label,
            "folder_name": folder_name,
            "tag_key": normalize_tag_key(str(item.get("tag_key") or "")),
        })
    return definitions


def _validate_kanban_column_payload(payload):
    label = normalize_name(payload.get("label"))
    tag_name = normalize_name(payload.get("tag_name")).lstrip("@")
    expression = (payload.get("expression") or "").strip()
    priority_filter = payload.get("priority_filter")
    try:
        width = int(payload.get("width", 1))
    except (TypeError, ValueError):
        width = 0
    if width not in (1, 2, 3):
        return None, None, None, None, None, "El ancho debe ser de 1, 2 o 3 columnas"
    if not label:
        return None, None, None, None, None, "Indica un nombre para la card"
    if priority_filter in ("1", "2", "3"):
        return label, "", "", int(priority_filter), width, None
    if priority_filter == "none":
        return label, "", "", "none", width, None
    if expression:
        if "(" in expression or ")" in expression:
            return None, None, None, None, None, "Las expresiones de las cards no admiten paréntesis"
        try:
            parse_filter_expression(_kanban_filter_parser_expression(expression))
        except FilterParseError as exc:
            return None, None, None, None, None, f"Expresión inválida: {exc}"
        if "@" not in expression:
            return None, None, None, None, None, "La expresión debe usar etiquetas con @"
        return label, "", expression, None, width, None
    if tag_name and re.fullmatch(r"[A-Za-z0-9_\-áéíóúÁÉÍÓÚñÑ]+", tag_name):
        return label, tag_name, f"@{tag_name}", None, width, None
    return None, None, None, None, None, "Indica una expresión de etiquetas o una prioridad"


def _validate_kanban_panel_name(payload):
    name = normalize_name(payload.get("name"))
    if not name:
        return None, "Indica un nombre para el panel"
    if len(name) > 60:
        return None, "El nombre del panel no puede superar 60 caracteres"
    return name, None
