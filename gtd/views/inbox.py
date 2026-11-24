"""Inbox: vista principal, procesar Inbox, archivar/vaciar."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, List, Optional, Tuple
from zoneinfo import ZoneInfo

from flask import flash, redirect, render_template, request, session, url_for

from subtasks import load_subtask_counts, load_subtasks_map

from gtd.utils import _today_madrid
from gtd.core import app
from gtd.db import commit, exec_sql, q, q1, rollback
from gtd.roles import (
    role_folder,
    role_folder_exists,
    role_folder_row,
    role_tag,
    role_tag_id,
    role_tag_sql_names,
)
from gtd.recurrence import complete_recurring_task_run, recurring_effective_due_sql
from gtd.calendar_sync import _mark_task_calendar_dirty
from gtd.common import load_tags_map, next_project_task_sort_order, subdb
from gtd.tasks_ops import complete_nonrecurring_task


@app.route("/")
def home():       
    recurring_due_join, effective_due_expr = recurring_effective_due_sql("t")

    inbox = q(
        f"SELECT t.id, t.title, t.location, t.notes, {effective_due_expr} AS due_date, t.due_time, t.completed_at, t.recurrence_rule "
        "FROM tasks t "
        f"{recurring_due_join}"
        "WHERE t.project_id IS NULL AND t.folder_id IS NULL AND t.archived=0 AND t.deleted_at IS NULL "
        f"ORDER BY ({effective_due_expr} IS NULL), {effective_due_expr} ASC, t.id DESC "
        "LIMIT 200"
    )

    # proyectos sin carpeta
    orphan_projects = q(
        "SELECT id, name, archived, archived_at "
        "FROM projects "
        "WHERE folder_id IS NULL AND deleted_at IS NULL "
        "ORDER BY archived ASC, name ASC"
    )

    inbox_ids = [r["id"] for r in inbox]
    tags_map = load_tags_map(inbox_ids) if inbox_ids else {}
    sub_counts = load_subtask_counts(subdb, inbox_ids)
    sub_map = load_subtasks_map(subdb, inbox_ids)

    return render_template(
        "home.html",
        inbox=inbox,
        tags_map=tags_map,
        sub_counts=sub_counts,
        sub_map=sub_map,
        orphan_projects=orphan_projects
    )

# ---------------- Procesar Inbox (GTD: aclarar y organizar, una tarea cada vez) ----------------

INBOX_PROCESS_SKIP_KEY = "inbox_process_skip"
INBOX_PROCESS_ACTIONS = {"done", "next", "delegate", "schedule", "move", "someday", "delete", "skip"}


def _inbox_process_skipped() -> List[int]:
    raw = session.get(INBOX_PROCESS_SKIP_KEY) or []
    return [int(x) for x in raw if str(x).isdigit()]


def _inbox_process_pending_sql(skipped: List[int]) -> Tuple[str, Tuple[Any, ...]]:
    where = (
        "WHERE t.project_id IS NULL AND t.folder_id IS NULL AND t.archived=0 "
        "AND t.deleted_at IS NULL AND t.completed_at IS NULL "
    )
    params: Tuple[Any, ...] = ()
    if skipped:
        where += "AND t.id NOT IN (" + ",".join(["%s"] * len(skipped)) + ") "
        params = tuple(skipped)
    return where, params


@app.route("/inbox/process")
def inbox_process():
    if request.args.get("restart"):
        session.pop(INBOX_PROCESS_SKIP_KEY, None)
        return redirect(url_for("inbox_process"))

    skipped = _inbox_process_skipped()
    where, params = _inbox_process_pending_sql(skipped)
    recurring_due_join, effective_due_expr = recurring_effective_due_sql("t")

    remaining = int((q1(f"SELECT COUNT(*) AS c FROM tasks t {where}", params) or {}).get("c", 0))
    task = q1(
        f"SELECT t.id, t.title, t.notes, t.location, t.priority, t.due_time, t.recurrence_rule, t.created_at, "
        f"{effective_due_expr} AS due_date "
        f"FROM tasks t {recurring_due_join}"
        f"{where}"
        "ORDER BY t.id ASC LIMIT 1",
        params,
    )
    tags = load_tags_map([int(task["id"])]).get(int(task["id"]), []) if task else []
    subtasks_count = load_subtask_counts(subdb, [int(task["id"])]).get(int(task["id"])) if task else None

    return render_template(
        "inbox_process.html",
        task=task,
        tags=tags,
        subtasks_count=subtasks_count,
        remaining=remaining,
        skipped_count=len(skipped),
        today=_today_madrid(),
        someday_folder_exists=role_folder_exists("someday"),
    )


@app.route("/inbox/process/<int:task_id>", methods=["POST"])
def inbox_process_action(task_id: int):
    action = (request.form.get("action") or "").strip().lower()
    if action not in INBOX_PROCESS_ACTIONS:
        flash("Acción no válida.", "error")
        return redirect(url_for("inbox_process"))

    task = q1(
        "SELECT t.id, t.title, t.completed_at, t.due_date, t.recurrence_rule, t.max_occurrences, "
        "t.project_id, t.folder_id, t.archived, rr.last_next_due_date "
        "FROM tasks t "
        "LEFT JOIN (SELECT task_id, MAX(next_due_date) AS last_next_due_date "
        "FROM recurring_task_runs GROUP BY task_id) rr ON rr.task_id=t.id "
        "WHERE t.id=%s AND t.deleted_at IS NULL",
        (task_id,),
    )
    if not task:
        flash("La tarea ya no existe.", "error")
        return redirect(url_for("inbox_process"))

    def skip_task() -> None:
        skipped = _inbox_process_skipped()
        if task_id not in skipped:
            skipped.append(task_id)
        session[INBOX_PROCESS_SKIP_KEY] = skipped

    if action == "skip":
        skip_task()
        return redirect(url_for("inbox_process"))

    # Destino opcional: "project:ID" o "folder:ID"
    dest_type, dest_id = None, None
    dest_raw = (request.form.get("destination") or "").strip()
    if ":" in dest_raw:
        kind, _, raw_id = dest_raw.partition(":")
        if kind in ("project", "folder") and raw_id.isdigit():
            dest_type, dest_id = kind, int(raw_id)

    due_date_value: Optional[date] = None
    if action == "schedule":
        try:
            due_date_value = date.fromisoformat((request.form.get("due_date") or "").strip())
        except ValueError:
            flash("Indica una fecha para programar la tarea.", "error")
            return redirect(url_for("inbox_process"))
    if action == "move" and not dest_type:
        flash("Elige un proyecto o carpeta de destino.", "error")
        return redirect(url_for("inbox_process"))
    if action == "someday":
        someday = role_folder_row("someday")
        if not someday:
            flash(f"No existe la carpeta «{role_folder('someday')}». Créala o configúrala en Admin.", "error")
            return redirect(url_for("inbox_process"))
        dest_type, dest_id = "folder", int(someday["id"])

    if dest_type == "project" and not q1(
        "SELECT id FROM projects WHERE id=%s AND archived=0 AND deleted_at IS NULL", (dest_id,)
    ):
        flash("Proyecto destino no válido.", "error")
        return redirect(url_for("inbox_process"))
    if dest_type == "folder" and not q1("SELECT id FROM folders WHERE id=%s", (dest_id,)):
        flash("Carpeta destino no válida.", "error")
        return redirect(url_for("inbox_process"))

    new_title = (request.form.get("title") or "").strip()
    messages = {
        "done": "Tarea completada.",
        "next": f"Marcada como @{role_tag('next_action')}.",
        "delegate": f"Delegada: etiquetada @{role_tag('waiting')}.",
        "schedule": "Tarea programada.",
        "move": "Tarea movida.",
        "someday": f"Movida a «{role_folder('someday')}».",
        "delete": "Tarea enviada a la papelera.",
    }

    try:
        if new_title and new_title != task["title"] and action != "delete":
            exec_sql("UPDATE tasks SET title=%s WHERE id=%s", (new_title, task_id))

        if action == "delete":
            exec_sql(
                "UPDATE tasks SET deleted_prev_archived=%s, deleted_at=NOW(), archived=1, "
                "archived_at=COALESCE(archived_at, NOW()) WHERE id=%s",
                (int(task.get("archived") or 0), task_id),
            )
        elif action == "done":
            now = datetime.now(ZoneInfo("Europe/Madrid")).replace(tzinfo=None)
            if task.get("recurrence_rule") and task.get("due_date"):
                complete_recurring_task_run(task_id, task, now, None)
            else:
                complete_nonrecurring_task(task_id, task, now)
        else:
            if action == "next":
                exec_sql("INSERT IGNORE INTO task_tags(task_id, tag_id) VALUES(%s,%s)",
                         (task_id, role_tag_id("next_action")))
            elif action == "delegate":
                exec_sql("INSERT IGNORE INTO task_tags(task_id, tag_id) VALUES(%s,%s)",
                         (task_id, role_tag_id("waiting")))
            elif action == "schedule":
                exec_sql("UPDATE tasks SET due_date=%s WHERE id=%s", (due_date_value, task_id))

            if dest_type == "project":
                exec_sql(
                    "UPDATE tasks SET project_id=%s, folder_id=NULL, sort_order=%s WHERE id=%s",
                    (dest_id, next_project_task_sort_order(dest_id), task_id),
                )
            elif dest_type == "folder":
                exec_sql("UPDATE tasks SET folder_id=%s, project_id=NULL WHERE id=%s", (dest_id, task_id))

        _mark_task_calendar_dirty(task_id)
        commit()
    except Exception as e:
        rollback()
        app.logger.exception("inbox_process_action failed for task_id=%s action=%s", task_id, action)
        flash(f"No se pudo procesar la tarea: {e}", "error")
        return redirect(url_for("inbox_process"))

    # Si sigue en el Inbox (p.ej. NextAction sin destino), no volver a mostrarla en esta sesión.
    if action in ("next", "delegate", "schedule") and not dest_type:
        skip_task()
    flash(messages[action], "ok")
    return redirect(url_for("inbox_process"))


@app.route("/inbox/archive_completed_tasks", methods=["POST"])
def inbox_archive_completed_tasks():
    periodic_names = tuple(role_tag_sql_names("periodic"))
    periodic_ph = ",".join(["LOWER(%s)"] * len(periodic_names))
    try:
        total_row = q1(
            "SELECT COUNT(*) AS c "
            "FROM tasks t "
            "WHERE t.folder_id IS NULL "
            "AND t.project_id IS NULL "
            "AND t.archived=0 "
            "AND t.deleted_at IS NULL "
            "AND t.completed_at IS NOT NULL "
            "AND (t.recurrence_rule IS NULL OR TRIM(t.recurrence_rule)='') "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM task_tags tt "
            "  JOIN tags tg ON tg.id=tt.tag_id "
            "  WHERE tt.task_id=t.id "
            "  AND LOWER(tg.name) IN (" + periodic_ph + ")"
            ")",
            periodic_names,
        )
        total_to_archive = int(total_row["c"]) if total_row else 0

        if total_to_archive > 0:
            exec_sql(
                "UPDATE tasks "
                "SET archived=1, archived_at=NOW() "
                "WHERE id IN ("
                "  SELECT id FROM ("
                "    SELECT t.id "
                "    FROM tasks t "
                "    WHERE t.folder_id IS NULL "
                "    AND t.project_id IS NULL "
                "    AND t.archived=0 "
                "    AND t.deleted_at IS NULL "
                "    AND t.completed_at IS NOT NULL "
                "    AND (t.recurrence_rule IS NULL OR TRIM(t.recurrence_rule)='') "
                "    AND NOT EXISTS ("
                "      SELECT 1 FROM task_tags tt "
                "      JOIN tags tg ON tg.id=tt.tag_id "
                "      WHERE tt.task_id=t.id "
                "      AND LOWER(tg.name) IN (" + periodic_ph + ")"
                "    )"
                "  ) AS eligible"
                ")",
                periodic_names,
            )

        commit()
        flash(f"{total_to_archive} tareas realizadas archivadas.", "ok")
    except Exception as e:
        rollback()
        flash(f"No se pudieron archivar las tareas realizadas: {e}", "error")

    return redirect(url_for("home"))


@app.route("/inbox/empty", methods=["POST"])
def inbox_empty():
    try:
        total_row = q1(
            "SELECT COUNT(*) AS c "
            "FROM tasks "
            "WHERE folder_id IS NULL "
            "AND project_id IS NULL "
            "AND deleted_at IS NULL",
        )
        total_to_trash = int(total_row["c"]) if total_row else 0

        if total_to_trash > 0:
            exec_sql(
                "UPDATE tasks "
                "SET deleted_prev_archived=archived, "
                "deleted_at=NOW(), "
                "archived=1, "
                "archived_at=COALESCE(archived_at, NOW()), "
                "calendar_local_changed_at=NOW(), "
                "calendar_sync_state=CASE WHEN google_event_id IS NOT NULL THEN 'pending_delete' ELSE 'none' END "
                "WHERE folder_id IS NULL "
                "AND project_id IS NULL "
                "AND deleted_at IS NULL",
            )

        commit()
        flash(f"{total_to_trash} tareas del Inbox enviadas a la papelera.", "ok")
    except Exception as e:
        rollback()
        flash(f"No se pudo vaciar el Inbox: {e}", "error")

    return redirect(url_for("home"))
