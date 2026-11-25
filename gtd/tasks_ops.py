"""Operaciones sobre tareas reutilizadas por varias rutas (completar, etc.)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from gtd.config import cfg_bool
from gtd.db import exec_sql, q1
from gtd.roles import role_tag, role_tag_id, task_has_role_tag
from gtd.kanban_board import _project_board_status_for_task


def complete_nonrecurring_task(task_id: int, task: Dict[str, Any], now: datetime) -> bool:
    """Marca como hecha una tarea no recurrente.

    Si tenía la etiqueta del rol `next_action`, se le quita y (si la promoción está
    activa, global o por proyecto) se etiqueta la siguiente tarea activa del proyecto.
    Devuelve True si se promocionó otra tarea a NextAction. No hace commit.
    """
    next_tag_name = role_tag("next_action")
    has_nextaction = task_has_role_tag(task_id, "next_action")

    # Quitar la NextAction antes de completar (en cualquier variante de mayúsculas/minúsculas)
    if has_nextaction:
        exec_sql(
            "DELETE tt FROM task_tags tt "
            "JOIN tags tg ON tg.id=tt.tag_id "
            "WHERE tt.task_id=%s AND LOWER(tg.name)=LOWER(%s)",
            (task_id, next_tag_name),
        )

    exec_sql("UPDATE tasks SET completed_at=%s WHERE id=%s", (now, task_id))
    _project_board_status_for_task(task_id, task.get("project_id"), "done")

    if not (has_nextaction and task.get("project_id")):
        return False

    promote_nextaction = cfg_bool(["app", "behavior", "promote_nextaction_on_complete"], default=True)
    # Override a nivel de proyecto (si está definido) tiene prioridad sobre el global
    proj_promo = q1("SELECT auto_promote_nextaction FROM projects WHERE id=%s", (task["project_id"],))
    if proj_promo and proj_promo.get("auto_promote_nextaction") is not None:
        promote_nextaction = bool(proj_promo["auto_promote_nextaction"])
    if not promote_nextaction:
        return False

    next_task = q1(
        "SELECT id "
        "FROM tasks "
        "WHERE project_id=%s "
        "AND archived=0 "
        "AND completed_at IS NULL "
        "AND id<>%s "
        "ORDER BY (sort_order IS NULL) ASC, sort_order ASC, (due_date IS NULL) ASC, due_date ASC, id ASC "
        "LIMIT 1",
        (task["project_id"], task_id),
    )
    if next_task and not task_has_role_tag(int(next_task["id"]), "next_action"):
        exec_sql(
            "INSERT IGNORE INTO task_tags(task_id, tag_id) VALUES(%s,%s)",
            (next_task["id"], role_tag_id("next_action")),
        )
        return True
    return False
