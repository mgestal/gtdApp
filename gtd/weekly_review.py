"""Estado de la revisión semanal: bloques revisados, inicio/fin y avisos.

Una revisión empieza al marcar el primer bloque y termina con «Terminar revisión».
Solo hay una en curso a la vez (``weekly_reviews.finished_at IS NULL``); las terminadas
forman el historial. Las fechas se guardan en hora local (Europe/Madrid), como las tareas.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from gtd.config import cfg_int
from gtd.db import exec_sql, q, q1
from gtd.roles import non_active_project_folder_ids, role_tag_filter

# Bloques de la revisión, en orden (1..9). Cada card de review.html lleva su casilla;
# los bloques 3 y 7 tienen dos cards que comparten clave.
BLOCK_KEYS = [
    "inbox",          # 1) Procesar inbox
    "next_actions",   # 2) Revisar NextActions
    "agenda_future",  # 3) Revisar agenda (eventos futuros)
    "agenda_past",    # 4) Revisar agenda (eventos pasados)
    "waiting",        # 5) Revisar EnEspera
    "projects",       # 6) Revisar Proyectos
    "someday",        # 7) Revisión ADTV / SomeTime / EnEspera
    "checklists",     # 8) Revisar listas de comprobación
    "brainstorm",     # 9) Brainstorming
]

# Días sin actividad para considerar un proyecto estancado (config: app.review.stalled_project_days)
STALLED_PROJECT_DAYS_DEFAULT = 14
STALLED_PROJECT_DAYS_PATH = ["app", "review", "stalled_project_days"]
STALLED_PROJECT_DAYS_MAX = 365


def _now() -> datetime:
    return datetime.now(ZoneInfo("Europe/Madrid")).replace(tzinfo=None, microsecond=0)


def parse_blocks(raw: Optional[str]) -> List[str]:
    """Claves válidas guardadas en ``checked_blocks``, en el orden de la checklist."""
    stored = {part.strip() for part in (raw or "").split(",")}
    return [key for key in BLOCK_KEYS if key in stored]


@dataclass
class ReviewState:
    """Lo que necesita la plantilla: revisión en curso y última terminada."""
    current_id: Optional[int] = None
    started_at: Optional[datetime] = None
    checked: List[str] = field(default_factory=list)
    last_finished_at: Optional[datetime] = None
    days_since_last: Optional[int] = None

    @property
    def in_progress(self) -> bool:
        return self.current_id is not None

    @property
    def total(self) -> int:
        return len(BLOCK_KEYS)


def days_between(earlier: datetime, today: date) -> int:
    return (today - earlier.date()).days


def current_review() -> Optional[Dict[str, Any]]:
    return q1("SELECT id, started_at, checked_blocks FROM weekly_reviews "
              "WHERE finished_at IS NULL ORDER BY id DESC LIMIT 1")


def load_state(today: Optional[date] = None) -> ReviewState:
    today = today or _now().date()
    state = ReviewState()
    current = current_review()
    if current:
        state.current_id = int(current["id"])
        state.started_at = current["started_at"]
        state.checked = parse_blocks(current["checked_blocks"])
    last = q1("SELECT finished_at FROM weekly_reviews WHERE finished_at IS NOT NULL "
              "ORDER BY finished_at DESC LIMIT 1")
    if last:
        state.last_finished_at = last["finished_at"]
        state.days_since_last = days_between(last["finished_at"], today)
    return state


def set_block(key: str, checked: bool) -> List[str]:
    """Marca o desmarca un bloque; si no hay revisión en curso, la empieza. No hace commit."""
    if key not in BLOCK_KEYS:
        raise ValueError("Bloque de revisión desconocido.")
    current = current_review()
    if current is None:
        if not checked:
            return []
        review_id = exec_sql("INSERT INTO weekly_reviews(started_at) VALUES(%s)", (_now(),))
        blocks: List[str] = []
    else:
        review_id = int(current["id"])
        blocks = parse_blocks(current["checked_blocks"])
    selected = set(blocks)
    if checked:
        selected.add(key)
    else:
        selected.discard(key)
    blocks = [k for k in BLOCK_KEYS if k in selected]
    exec_sql("UPDATE weekly_reviews SET checked_blocks=%s WHERE id=%s", (",".join(blocks), review_id))
    return blocks


def finish_review() -> Optional[datetime]:
    """Cierra la revisión en curso; devuelve la fecha de fin o None si no había. No hace commit."""
    current = current_review()
    if current is None:
        return None
    finished = _now()
    exec_sql("UPDATE weekly_reviews SET finished_at=%s WHERE id=%s", (finished, current["id"]))
    return finished


def projects_without_issues(projects: List[Dict[str, Any]], *flagged: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """``projects`` sin los que aparecen en alguna de las listas de avisos (por id)."""
    flagged_ids = {int(p["id"]) for group in flagged for p in group}
    return [p for p in projects if int(p["id"]) not in flagged_ids]


def projects_without_next_action() -> List[Dict[str, Any]]:
    """Proyectos activos con tareas pendientes pero ninguna etiquetada como NextAction.

    Excluye las carpetas de roles que no cuentan como proyectos activos
    (Rutinas, Agenda, Sometime, ADTV, Seguimiento) y sus subcarpetas.
    """
    next_filter, next_params = role_tag_filter("next_action", task_alias="na")
    excluded_sql, excluded = _excluded_project_folders_sql()
    return q(
        "SELECT p.id, p.name, f.name AS folder_name, COUNT(t.id) AS pending "
        "FROM projects p "
        "LEFT JOIN folders f ON f.id=p.folder_id "
        "JOIN tasks t ON t.project_id=p.id AND t.completed_at IS NULL AND t.archived=0 AND t.deleted_at IS NULL "
        "WHERE p.archived=0 AND p.deleted_at IS NULL "
        + excluded_sql +
        "AND NOT EXISTS (SELECT 1 FROM tasks na WHERE na.project_id=p.id AND na.completed_at IS NULL "
        "AND na.archived=0 AND na.deleted_at IS NULL " + next_filter + ") "
        "GROUP BY p.id, p.name, f.name "
        "ORDER BY p.name",
        tuple(excluded) + tuple(next_params),
    ) or []


def stalled_project_days() -> int:
    return cfg_int(STALLED_PROJECT_DAYS_PATH, STALLED_PROJECT_DAYS_DEFAULT, 1, STALLED_PROJECT_DAYS_MAX)


def _excluded_project_folders_sql() -> tuple:
    excluded = sorted(non_active_project_folder_ids())
    if not excluded:
        return "", ()
    return ("AND (p.folder_id IS NULL OR p.folder_id NOT IN (" + ",".join(["%s"] * len(excluded)) + ")) ",
            tuple(excluded))


def stalled_projects(days: Optional[int] = None, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """Proyectos activos con tareas pendientes y sin actividad en ``days`` días.

    Actividad: crear el proyecto o crear/completar alguna de sus tareas (las archivadas
    cuentan; las de la papelera no). Excluye las mismas carpetas que
    :func:`projects_without_next_action`. Cada fila lleva ``last_activity`` y ``idle_days``.
    """
    days = days or stalled_project_days()
    now = now or _now()
    cutoff = now - timedelta(days=days)
    excluded_sql, excluded_params = _excluded_project_folders_sql()
    rows = q(
        "SELECT p.id, p.name, f.name AS folder_name, a.pending, "
        "GREATEST(p.created_at, COALESCE(a.last_created, p.created_at), "
        "COALESCE(a.last_completed, p.created_at), COALESCE(a.last_recurring, p.created_at)) AS last_activity "
        "FROM projects p "
        "LEFT JOIN folders f ON f.id=p.folder_id "
        "JOIN (SELECT t.project_id, "
        "SUM(t.completed_at IS NULL AND t.archived=0) AS pending, "
        "MAX(t.created_at) AS last_created, MAX(t.completed_at) AS last_completed, "
        "MAX(t.last_completed_at) AS last_recurring "
        "FROM tasks t WHERE t.project_id IS NOT NULL AND t.deleted_at IS NULL GROUP BY t.project_id"
        ") a ON a.project_id=p.id "
        "WHERE p.archived=0 AND p.deleted_at IS NULL AND a.pending > 0 "
        + excluded_sql +
        "HAVING last_activity < %s "
        "ORDER BY last_activity ASC, p.name",
        excluded_params + (cutoff,),
    ) or []
    for row in rows:
        last = row.get("last_activity")
        row["pending"] = int(row.get("pending") or 0)
        row["idle_days"] = days_between(last, now.date()) if last else None
    return rows
