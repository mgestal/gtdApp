"""Historial de tareas periódicas."""
from __future__ import annotations

from typing import Any, Dict, List

from flask import abort, flash, redirect, render_template, request

from gtd.config import cfg_int
from gtd.utils import safe_next_url
from gtd.core import app
from gtd.db import commit, exec_sql, q, q1, rollback
from gtd.roles import role_tag_sql_names
from gtd.recurrence import recurring_effective_due_sql
from gtd.calendar_sync import _mark_task_calendar_dirty


@app.route("/periodic/history")
def periodic_history():
    try:
        tag_id = int(request.args.get("tag_id", "0"))
    except ValueError:
        tag_id = 0

    tag = None
    if tag_id > 0:
        tag = q1("SELECT id, name FROM tags WHERE id=%s", (tag_id,))

    if not tag:
        # Acceso directo sin id: usa la primera etiqueta periódica disponible.
        tag = q1(
            "SELECT id, name FROM tags "
            "WHERE LOWER(name) IN (" + ",".join(["%s"] * len(role_tag_sql_names("periodic"))) + ") "
            "ORDER BY id ASC "
            "LIMIT 1",
            tuple(role_tag_sql_names("periodic")),
        )

    if not tag:
        abort(404)

    tag_id = int(tag["id"])

    per_page = cfg_int(["app", "pagination", "tag_detail_per_page"], default=25, min_v=5, max_v=500)
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    page = max(page, 1)
    offset = (page - 1) * per_page
    recurring_due_join, effective_due_expr = recurring_effective_due_sql("t")

    total_row = q1(
        "SELECT COUNT(*) AS c "
        "FROM task_tags tt "
        "JOIN tasks t ON t.id=tt.task_id "
        "WHERE tt.tag_id=%s "
        "AND t.recurrence_rule IS NOT NULL "
        "AND TRIM(t.recurrence_rule) <> ''",
        (tag_id,),
    )
    total = int(total_row["c"]) if total_row else 0
    pages = max(1, (total + per_page - 1) // per_page)

    if page > pages:
        page = pages
        offset = (page - 1) * per_page

    rows = q(
        f"SELECT t.id, t.title, {effective_due_expr} AS due_date, t.due_time, t.recurrence_rule, "
        "p.id AS project_id, p.name AS project_name, "
        "fd.id AS folder_id, fd.name AS folder_name "
        "FROM task_tags tt "
        "JOIN tasks t ON t.id=tt.task_id "
        "LEFT JOIN projects p ON p.id=t.project_id "
        f"{recurring_due_join}"
        "LEFT JOIN folders fd ON fd.id=COALESCE(t.folder_id, p.folder_id) "
        "WHERE tt.tag_id=%s "
        "AND t.recurrence_rule IS NOT NULL "
        "AND TRIM(t.recurrence_rule) <> '' "
        f"ORDER BY ({effective_due_expr} IS NULL) ASC, {effective_due_expr} ASC, t.id DESC "
        "LIMIT %s OFFSET %s",
        (tag_id, per_page, offset),
    )

    task_ids = [int(r["id"]) for r in rows]
    runs_map: Dict[int, List[Dict[str, Any]]] = {}

    if task_ids:
        placeholders = ",".join(["%s"] * len(task_ids))
        run_rows = q(
            f"SELECT task_id, executed_at, previous_due_date, next_due_date "
            f"FROM recurring_task_runs "
            f"WHERE task_id IN ({placeholders}) "
            f"ORDER BY executed_at DESC",
            tuple(task_ids),
        )
        for rr in run_rows:
            tid = int(rr["task_id"])
            runs_map.setdefault(tid, []).append(rr)

    return render_template(
        "periodic_history.html",
        tag=tag,
        rows=rows,
        runs_map=runs_map,
        page=page,
        pages=pages,
        total=total,
        per_page=per_page,
    )


@app.route("/tasks/<int:task_id>/periodic-history")
def task_periodic_history(task_id: int):
    recurring_due_join, effective_due_expr = recurring_effective_due_sql("t")
    task = q1(
        f"SELECT t.id, t.title, {effective_due_expr} AS due_date, t.due_time, t.recurrence_rule, "
        "p.id AS project_id, p.name AS project_name, "
        "fd.id AS folder_id, fd.name AS folder_name "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        f"{recurring_due_join}"
        "LEFT JOIN folders fd ON fd.id=COALESCE(t.folder_id, p.folder_id) "
        "WHERE t.id=%s",
        (task_id,),
    )
    if not task:
        abort(404)

    if not (task.get("recurrence_rule") and str(task.get("recurrence_rule")).strip()):
        flash("La tarea no es periódica.", "error")
        return redirect(safe_next_url(request.args.get("next"), "home"))

    per_page = cfg_int(["app", "pagination", "periodic_history_per_page"], default=20, min_v=5, max_v=500)
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    page = max(page, 1)
    offset = (page - 1) * per_page

    total_row = q1(
        "SELECT COUNT(*) AS c FROM recurring_task_runs WHERE task_id=%s",
        (task_id,),
    )
    total = int(total_row["c"]) if total_row else 0
    pages = max(1, (total + per_page - 1) // per_page)

    if page > pages:
        page = pages
        offset = (page - 1) * per_page

    runs = q(
        "SELECT executed_at, previous_due_date, next_due_date "
        "FROM recurring_task_runs "
        "WHERE task_id=%s "
        "ORDER BY executed_at DESC "
        "LIMIT %s OFFSET %s",
        (task_id, per_page, offset),
    )

    next_url = safe_next_url(request.args.get("next"), "home")

    return render_template(
        "task_periodic_history.html",
        task=task,
        runs=runs,
        page=page,
        pages=pages,
        total=total,
        per_page=per_page,
        next_url=next_url,
    )


@app.route("/tasks/<int:task_id>/periodic-history/undo-last", methods=["POST"])
def task_periodic_history_undo_last(task_id: int):
    next_url = safe_next_url(request.form.get("next"), "task_periodic_history", task_id=task_id)

    task = q1(
        "SELECT id, due_date, recurrence_rule "
        "FROM tasks "
        "WHERE id=%s",
        (task_id,),
    )
    if not task:
        abort(404)

    if not (task.get("recurrence_rule") and str(task.get("recurrence_rule")).strip()):
        flash("La tarea no es periódica.", "error")
        return redirect(next_url)

    last_run = q1(
        "SELECT id, executed_at, previous_due_date, next_due_date "
        "FROM recurring_task_runs "
        "WHERE task_id=%s "
        "ORDER BY executed_at DESC, id DESC "
        "LIMIT 1",
        (task_id,),
    )
    if not last_run:
        flash("No hay ejecuciones para deshacer.", "error")
        return redirect(next_url)

    try:
        exec_sql(
            "DELETE FROM recurring_task_runs WHERE id=%s",
            (int(last_run["id"]),),
        )

        remaining_last_run = q1(
            "SELECT executed_at, next_due_date "
            "FROM recurring_task_runs "
            "WHERE task_id=%s "
            "ORDER BY executed_at DESC, id DESC "
            "LIMIT 1",
            (task_id,),
        )

        restored_due = last_run.get("previous_due_date")
        if restored_due is None and remaining_last_run and remaining_last_run.get("next_due_date") is not None:
            restored_due = remaining_last_run.get("next_due_date")

        restored_last_completed_at = remaining_last_run.get("executed_at") if remaining_last_run else None

        exec_sql(
            "UPDATE tasks "
            "SET due_date=%s, last_completed_at=%s, completed_at=NULL "
            "WHERE id=%s",
            (restored_due, restored_last_completed_at, task_id),
        )

        _mark_task_calendar_dirty(task_id)
        commit()
        flash("Última ejecución deshecha y fecha recalculada.", "ok")
    except Exception as e:
        rollback()
        flash(f"No se pudo deshacer la última ejecución: {e}", "error")

    return redirect(next_url)
