# subtasks.py
# -----------------------------------------------------------------------------
# Subtareas para GTD App
#
# - Las subtareas están ligadas únicamente a una tarea madre (tasks.id).
# - No intervienen en búsqueda/filtros/agenda/hoy como entradas propias:
#   solo se muestran anidadas bajo su tarea madre.
# - Si la tarea madre está completada, las subtareas se muestran difuminadas
#   (estilo visual), pero NO se marcan como realizadas ni se rellena completed_at.
#
# Este módulo NO asume un ORM. Usa funciones DB que debes pasarle desde app.py:
#   q(sql, params), q1(sql, params), exec_sql(sql, params), commit(), rollback()
#
# Integra también helpers para cargar subtareas y contadores (done/total).
# -----------------------------------------------------------------------------

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, date
from typing import Any, Callable, Dict, List, Optional, Tuple

from flask import abort, flash, redirect, render_template, request, url_for
from zoneinfo import ZoneInfo


# Tipos de funciones DB (inyectadas desde app.py)
QFn = Callable[[str, Tuple[Any, ...]], List[Dict[str, Any]]]
Q1Fn = Callable[[str, Tuple[Any, ...]], Optional[Dict[str, Any]]]
ExecFn = Callable[[str, Tuple[Any, ...]], Any]
CommitFn = Callable[[], None]
RollbackFn = Callable[[], None]


@dataclass
class DB:
    q: QFn
    q1: Q1Fn
    exec_sql: ExecFn
    commit: CommitFn
    rollback: RollbackFn


def _now_madrid_naive() -> datetime:
    # Guardamos naive en DB para mantener consistencia con el resto de la app
    return datetime.now(ZoneInfo("Europe/Madrid")).replace(tzinfo=None)


def _parse_date_ddmmyyyy(s: str) -> Optional[date]:
    s = (s or "").strip()
    if not s:
        return None
    # Permitimos dd-mm-aaaa (UI)
    return datetime.strptime(s, "%d-%m-%Y").date()


_BULLET_RE = re.compile(r"^(?:\s*(?:[-*•+]\s+|\d+[.)]\s+|\[[ xX]?\]\s*))+")

# Fecha opcional al final de la línea. Con «*» vale cualquier token de la entrada rápida
# (*15-12, *15/12/2026, *hoy, *mañana, *lunes, *+3). Sin «*» solo fechas numéricas, hoy y
# mañana, para no comerse palabras del título («Preparar la reunión del lunes»).
_TRAILING_DATE_RE = re.compile(
    r"\s+(?:\*(?P<star>\d{1,2}[-/]\d{1,2}(?:[-/]\d{4})?|hoy|mañana|lunes|martes|miércoles|miercoles"
    r"|jueves|viernes|sábado|sabado|domingo|\+\d+)"
    r"|(?P<bare>\d{1,2}[-/]\d{1,2}(?:[-/]\d{4})?|hoy|mañana))\s*$",
    re.IGNORECASE,
)


def parse_subtask_lines(text: str) -> List[Tuple[str, Optional[date]]]:
    """Convierte un texto multilínea en [(título, fecha|None), ...].

    - Una subtarea por línea; las líneas vacías se ignoran.
    - Quita viñetas iniciales: -, *, •, +, «1.», «1)», «[ ]», «[x]».
    - Fecha opcional al final de la línea (ver _TRAILING_DATE_RE).
    Lanza ValueError con la línea afectada si una fecha no es válida (p.ej. 31-02).
    """
    from gtd.quick_entry import parse_due_token

    out: List[Tuple[str, Optional[date]]] = []
    for raw in (text or "").splitlines():
        line = _BULLET_RE.sub("", raw).strip()
        if not line:
            continue
        due: Optional[date] = None
        m = _TRAILING_DATE_RE.search(line)
        if m:
            token = m.group("star") or m.group("bare")
            try:
                due = parse_due_token(token)
            except ValueError:
                raise ValueError(f"Fecha inválida en «{line}».")
            line = line[:m.start()].strip()
        if line:
            out.append((line, due))
    return out


def _safe_int(s: str) -> Optional[int]:
    s = (s or "").strip()
    if not s:
        return None
    return int(s)


# -----------------------------------------------------------------------------
# Public helpers (para usar desde app.py en listados)
# -----------------------------------------------------------------------------

def load_subtasks_map(db: DB, task_ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
    """
    Devuelve {task_id: [subtask_row, ...]}.
    """
    if not task_ids:
        return {}

    # IN (%s,%s,...) seguro (parametrizado)
    placeholders = ",".join(["%s"] * len(task_ids))
    rows = db.q(
        f"SELECT id, task_id, title, description, due_date, completed_at, created_at "
        f"FROM subtasks "
        f"WHERE task_id IN ({placeholders}) "
        f"ORDER BY task_id ASC, (completed_at IS NOT NULL) ASC, (due_date IS NULL) ASC, due_date ASC, id ASC",
        tuple(task_ids),
    )

    out: Dict[int, List[Dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r["task_id"], []).append(r)
    return out


def load_subtask_counts(db: DB, task_ids: List[int]) -> Dict[int, Dict[str, int]]:
    """
    Devuelve {task_id: {'total': X, 'done': Y}}.
    """
    if not task_ids:
        return {}

    placeholders = ",".join(["%s"] * len(task_ids))
    rows = db.q(
        f"SELECT task_id, COUNT(*) AS total, "
        f"SUM(CASE WHEN completed_at IS NOT NULL THEN 1 ELSE 0 END) AS done "
        f"FROM subtasks "
        f"WHERE task_id IN ({placeholders}) "
        f"GROUP BY task_id",
        tuple(task_ids),
    )

    out: Dict[int, Dict[str, int]] = {}
    for r in rows:
        out[int(r["task_id"])] = {"total": int(r["total"]), "done": int(r["done"] or 0)}
    return out


# -----------------------------------------------------------------------------
# Route registration
# -----------------------------------------------------------------------------

def register_subtask_routes(app, db: DB) -> None:
    """
    Registra rutas de subtareas en Flask.
    """

    @app.route("/tasks/<int:task_id>/subtasks", methods=["GET"])
    def subtasks_manage(task_id: int):
        """
        Pantalla de gestión de subtareas (opcional).
        Si prefieres gestionarlas dentro de task_edit.html, puedes no usar esta vista.
        """
        task = db.q1("SELECT id, title, completed_at FROM tasks WHERE id=%s", (task_id,))
        if not task:
            abort(404)

        subs = db.q(
            "SELECT id, task_id, title, description, due_date, completed_at, created_at "
            "FROM subtasks WHERE task_id=%s "
            "ORDER BY (completed_at IS NOT NULL) ASC, (due_date IS NULL) ASC, due_date ASC, id ASC",
            (task_id,),
        )
        counts = load_subtask_counts(db, [task_id]).get(task_id, {"total": 0, "done": 0})
        return render_template("subtasks_manage.html", task=task, subs=subs, counts=counts)

    @app.route("/tasks/<int:task_id>/subtasks/add", methods=["POST"])
    def subtask_add(task_id: int):
        task = db.q1("SELECT id FROM tasks WHERE id=%s", (task_id,))
        if not task:
            abort(404)

        back = request.referrer or url_for("task_edit", task_id=task_id)
        description = (request.form.get("description") or "").strip() or None
        due_raw = (request.form.get("due_date") or "").strip()

        try:
            items = parse_subtask_lines(request.form.get("title") or "")
        except ValueError as e:
            flash(f"{e} No se ha creado ninguna subtarea.", "error")
            return redirect(back)

        if not items:
            flash("El título de la subtarea es obligatorio.", "error")
            return redirect(back)

        if description and len(items) > 1:
            flash("La descripción solo se puede indicar al añadir una única subtarea.", "error")
            return redirect(back)

        try:
            default_due = _parse_date_ddmmyyyy(due_raw) if due_raw else None
        except Exception:
            flash("Fecha inválida. Usa dd-mm-aaaa.", "error")
            return redirect(back)

        try:
            for title, due_date in items:
                db.exec_sql(
                    "INSERT INTO subtasks(task_id, title, description, due_date) VALUES(%s,%s,%s,%s)",
                    (task_id, title, description, due_date or default_due),
                )
            db.commit()
            flash("Subtarea creada." if len(items) == 1 else f"{len(items)} subtareas creadas.", "ok")
        except Exception as e:
            db.rollback()
            flash(f"No se pudo crear la subtarea: {e}", "error")

        return redirect(request.referrer or url_for("task_edit", task_id=task_id))

    @app.route("/subtasks/<int:subtask_id>/update", methods=["POST"])
    def subtask_update(subtask_id: int):
        st = db.q1("SELECT id, task_id FROM subtasks WHERE id=%s", (subtask_id,))
        if not st:
            abort(404)

        title = (request.form.get("title") or "").strip()
        description = (request.form.get("description") or "").strip() or None
        due_raw = (request.form.get("due_date") or "").strip()

        if not title:
            flash("El título de la subtarea es obligatorio.", "error")
            return redirect(request.referrer or url_for("task_edit", task_id=st["task_id"]))

        try:
            due_date = _parse_date_ddmmyyyy(due_raw) if due_raw else None
        except Exception:
            flash("Fecha inválida. Usa dd-mm-aaaa.", "error")
            return redirect(request.referrer or url_for("task_edit", task_id=st["task_id"]))

        try:
            db.exec_sql(
                "UPDATE subtasks SET title=%s, description=%s, due_date=%s WHERE id=%s",
                (title, description, due_date, subtask_id),
            )
            db.commit()
            flash("Subtarea actualizada.", "ok")
        except Exception as e:
            db.rollback()
            flash(f"No se pudo actualizar: {e}", "error")

        return redirect(request.referrer or url_for("task_edit", task_id=st["task_id"]))

    @app.route("/subtasks/<int:subtask_id>/toggle", methods=["POST"])
    def subtask_toggle(subtask_id: int):
        st = db.q1("SELECT id, task_id, completed_at FROM subtasks WHERE id=%s", (subtask_id,))
        if not st:
            abort(404)

        try:
            if st["completed_at"]:
                db.exec_sql("UPDATE subtasks SET completed_at=NULL WHERE id=%s", (subtask_id,))
            else:
                db.exec_sql("UPDATE subtasks SET completed_at=%s WHERE id=%s", (_now_madrid_naive(), subtask_id))
            db.commit()
        except Exception as e:
            db.rollback()
            flash(f"No se pudo cambiar el estado: {e}", "error")

        return redirect(request.referrer or url_for("task_edit", task_id=st["task_id"]))

    @app.route("/subtasks/<int:subtask_id>/delete", methods=["POST"])
    def subtask_delete(subtask_id: int):
        st = db.q1("SELECT id, task_id FROM subtasks WHERE id=%s", (subtask_id,))
        if not st:
            abort(404)

        try:
            db.exec_sql("DELETE FROM subtasks WHERE id=%s", (subtask_id,))
            db.commit()
            flash("Subtarea borrada.", "ok")
        except Exception as e:
            db.rollback()
            flash(f"No se pudo borrar: {e}", "error")

        return redirect(request.referrer or url_for("task_edit", task_id=st["task_id"]))
