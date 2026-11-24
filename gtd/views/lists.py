"""Listas de tareas: Hoy, Próximo, Semana, NextActions, calendario mensual."""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from typing import List, Optional

from flask import redirect, render_template, request, url_for

from subtasks import load_subtask_counts, load_subtasks_map

from gtd.config import cfg_int
from gtd.utils import _today_madrid
from gtd.core import app
from gtd.db import q, q1
from gtd.roles import role_tag
from gtd.recurrence import recurring_effective_due_sql
from gtd.common import load_tags_map, subdb


@app.route("/agenda")
def agenda_legacy():
    return redirect(url_for("proximo"), code=301)


@app.route("/proximo")
def proximo():

    per_page = cfg_int(["app", "pagination", "agenda_per_page"], default=25, min_v=5, max_v=500)

    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    page = max(page, 1)
    offset = (page - 1) * per_page

    recurring_due_join = (
        "LEFT JOIN ("
        "SELECT task_id, MAX(next_due_date) AS last_next_due_date "
        "FROM recurring_task_runs "
        "GROUP BY task_id"
        ") rr ON rr.task_id=t.id "
    )
    effective_due_expr = (
        "CASE "
        "WHEN t.recurrence_rule IS NOT NULL AND TRIM(t.recurrence_rule) <> '' "
        "AND rr.last_next_due_date IS NOT NULL "
        "AND (t.due_date IS NULL OR rr.last_next_due_date > t.due_date) "
        "THEN rr.last_next_due_date "
        "ELSE t.due_date "
        "END"
    )

    # Total de tareas con fecha (si tu agenda incluye solo tareas con due_date)
    total_row = q1(
        f"SELECT COUNT(*) AS c FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        f"{recurring_due_join}"
        f"WHERE {effective_due_expr} IS NOT NULL AND t.completed_at IS NULL "
        "AND t.deleted_at IS NULL "
        "AND (t.project_id IS NULL OR p.archived = 0)"
    )
    total = int(total_row["c"]) if total_row else 0
    pages = max(1, (total + per_page - 1) // per_page)

    rows = q(
        f"SELECT t.id, t.title, t.location, t.notes, {effective_due_expr} AS due_date, t.due_time, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.id AS folder_id, fd.name AS folder_name "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        f"{recurring_due_join}"
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        f"WHERE {effective_due_expr} IS NOT NULL "
        "AND t.completed_at IS NULL "
        "AND t.deleted_at IS NULL "
        "AND (t.project_id IS NULL OR p.archived = 0) "
        f"ORDER BY (t.completed_at IS NOT NULL) ASC, {effective_due_expr} ASC, (t.due_time IS NULL) ASC, t.due_time ASC, t.id DESC "
        "LIMIT %s OFFSET %s",
        (per_page, offset),
    )
    
    
    task_ids = [r["id"] for r in rows]
    tags_map = load_tags_map(task_ids) if task_ids else {}
    sub_counts = load_subtask_counts(subdb, task_ids)
    sub_map = load_subtasks_map(subdb, task_ids)

    today_d = _today_madrid()

    return render_template(
        "proximo.html",
        rows=rows,
        tags_map=tags_map,
        sub_counts=sub_counts,
        sub_map=sub_map,
        today_d=today_d,
        page=page,
        pages=pages,
        total=total,
        per_page=per_page,
    )

@app.route("/today")
def today():
    today_d = _today_madrid()

    recurring_due_join = (
        "LEFT JOIN ("
        "SELECT task_id, MAX(next_due_date) AS last_next_due_date "
        "FROM recurring_task_runs "
        "GROUP BY task_id"
        ") rr ON rr.task_id=t.id "
    )
    effective_due_expr = (
        "CASE "
        "WHEN t.recurrence_rule IS NOT NULL AND TRIM(t.recurrence_rule) <> '' "
        "AND rr.last_next_due_date IS NOT NULL "
        "AND (t.due_date IS NULL OR rr.last_next_due_date > t.due_date) "
        "THEN rr.last_next_due_date "
        "ELSE t.due_date "
        "END"
    )

    pending_rows = q(
        f"SELECT t.id, t.title, t.location, t.notes, {effective_due_expr} AS due_date, t.due_time, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.id AS folder_id, fd.name AS folder_name "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        f"{recurring_due_join}"
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        f"WHERE {effective_due_expr}=%s AND t.completed_at IS NULL "
        "AND t.deleted_at IS NULL "
        "AND (t.project_id IS NULL OR p.archived = 0) "
        "ORDER BY t.id DESC",
        (today_d,)
    )

    overdue_rows = q(
        f"SELECT t.id, t.title, t.location, t.notes, {effective_due_expr} AS due_date, t.due_time, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.id AS folder_id, fd.name AS folder_name "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        f"{recurring_due_join}"
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        f"WHERE {effective_due_expr} < %s AND t.completed_at IS NULL "
        "AND t.deleted_at IS NULL "
        "AND (t.project_id IS NULL OR p.archived = 0) "
        f"ORDER BY {effective_due_expr} ASC, t.id DESC",
        (today_d,)
    )

    done_rows = q(
        "("
        "SELECT t.id, t.title, t.location, t.notes, t.due_date, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.id AS folder_id, fd.name AS folder_name "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "WHERE t.completed_at IS NOT NULL AND DATE(t.completed_at)=%s "
        "AND t.deleted_at IS NULL "
        "UNION ALL "
        "SELECT t.id, t.title, t.location, t.notes, t.due_date, t.last_completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.id AS folder_id, fd.name AS folder_name "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "WHERE t.last_completed_at IS NOT NULL AND DATE(t.last_completed_at)=%s "
        "AND t.recurrence_rule IS NOT NULL AND TRIM(t.recurrence_rule) <> '' "
        "AND t.deleted_at IS NULL "
        ") "
        "ORDER BY completed_at DESC, id DESC",
        (today_d, today_d)
    )

    all_ids = [r["id"] for r in pending_rows] + [r["id"] for r in overdue_rows] + [r["id"] for r in done_rows]
    tags_map = load_tags_map(all_ids) if all_ids else {}
    sub_counts = load_subtask_counts(subdb, all_ids)
    sub_map = load_subtasks_map(subdb, all_ids)

    return render_template(
        "today.html",
        pending_rows=pending_rows,
        overdue_rows=overdue_rows,
        done_rows=done_rows,
        tags_map=tags_map,
        sub_counts=sub_counts,
        sub_map=sub_map,
        today=today_d,
    )


@app.route("/week")
def week():

    from datetime import timedelta

    today_d = _today_madrid()

    # lunes de la semana actual
    monday_d = today_d - timedelta(days=today_d.weekday())

    # domingo de la semana actual
    sunday_d = monday_d + timedelta(days=6)

    recurring_due_join = (
        "LEFT JOIN ("
        "SELECT task_id, MAX(next_due_date) AS last_next_due_date "
        "FROM recurring_task_runs "
        "GROUP BY task_id"
        ") rr ON rr.task_id=t.id "
    )
    effective_due_expr = (
        "CASE "
        "WHEN t.recurrence_rule IS NOT NULL AND TRIM(t.recurrence_rule) <> '' "
        "AND rr.last_next_due_date IS NOT NULL "
        "AND (t.due_date IS NULL OR rr.last_next_due_date > t.due_date) "
        "THEN rr.last_next_due_date "
        "ELSE t.due_date "
        "END"
    )

    overdue_rows = q(
        f"SELECT t.id, t.title, t.location, t.notes, {effective_due_expr} AS due_date, t.due_time, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        f"{recurring_due_join}"
        "LEFT JOIN folders fd ON fd.id = COALESCE(t.folder_id, p.folder_id) "
        f"WHERE {effective_due_expr} IS NOT NULL "
        f"AND {effective_due_expr} < %s "
        "AND t.completed_at IS NULL "
        "AND t.deleted_at IS NULL "
        "AND (t.project_id IS NULL OR p.archived = 0) "
        f"ORDER BY {effective_due_expr} ASC, t.id DESC",
        (monday_d,)
    )

    rows = q(
        "SELECT * FROM ("
        f"SELECT t.id, t.title, t.location, t.notes, {effective_due_expr} AS due_date, t.due_time, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        f"{recurring_due_join}"
        "LEFT JOIN folders fd ON fd.id = COALESCE(t.folder_id, p.folder_id) "
        f"WHERE {effective_due_expr} IS NOT NULL "
        f"AND {effective_due_expr} >= %s "
        f"AND {effective_due_expr} <= %s "
        "AND t.completed_at IS NULL "
        "AND t.deleted_at IS NULL "
        "AND (t.project_id IS NULL OR p.archived = 0) "
        "UNION ALL "
        f"SELECT t.id, t.title, t.location, t.notes, {effective_due_expr} AS due_date, t.due_time, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        f"{recurring_due_join}"
        "LEFT JOIN folders fd ON fd.id = COALESCE(t.folder_id, p.folder_id) "
        f"WHERE {effective_due_expr} IS NOT NULL "
        f"AND {effective_due_expr} >= %s "
        f"AND {effective_due_expr} <= %s "
        "AND t.completed_at IS NOT NULL "
        "AND t.deleted_at IS NULL "
        ") AS week_rows "
        "ORDER BY (completed_at IS NOT NULL) ASC, due_date ASC, id DESC",
        (monday_d, sunday_d, monday_d, sunday_d)
    )

    task_ids = [r["id"] for r in overdue_rows] + [r["id"] for r in rows]
    tags_map = load_tags_map(task_ids) if task_ids else {}
    sub_counts = load_subtask_counts(subdb, task_ids)
    sub_map = load_subtasks_map(subdb, task_ids)

    return render_template(
        "week.html",
        overdue_rows=overdue_rows,
        rows=rows,
        tags_map=tags_map,
        today=today_d,
        sunday=sunday_d,
        monday=monday_d,
    )


def _parse_yyyy_mm_dd(value: Optional[str], default: date) -> date:
    if not value:
        return default
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except Exception:
        return default


def _build_month_weeks(year: int, month: int) -> List[List[Optional[date]]]:
    cal = calendar.Calendar(firstweekday=0)
    weeks: List[List[Optional[date]]] = []
    for week in cal.monthdatescalendar(year, month):
        row: List[Optional[date]] = []
        for d in week:
            row.append(d if d.month == month else None)
        weeks.append(row)
    return weeks


@app.route("/calendar", endpoint="calendar")
def calendar_view():
    view = (request.args.get("view") or "month").strip().lower()
    if view not in ("day", "week", "next7", "month"):
        view = "month"

    today_date = _today_madrid()
    selected_date = _parse_yyyy_mm_dd(request.args.get("date"), today_date)

    if view == "month":
        start_date = selected_date.replace(day=1)
        last_day = calendar.monthrange(selected_date.year, selected_date.month)[1]
        end_date = selected_date.replace(day=last_day)
        prev_date = (start_date - timedelta(days=1)).replace(day=1)
        next_month_first = start_date + timedelta(days=last_day)
        next_date = next_month_first.replace(day=1)
        week_days = None
    elif view == "week":
        start_date = selected_date - timedelta(days=selected_date.weekday())
        end_date = start_date + timedelta(days=6)
        prev_date = start_date - timedelta(days=7)
        next_date = start_date + timedelta(days=7)
        week_days = [start_date + timedelta(days=i) for i in range(7)]
    elif view == "next7":
        start_date = selected_date
        end_date = selected_date + timedelta(days=7)
        prev_date = selected_date - timedelta(days=7)
        next_date = selected_date + timedelta(days=7)
        week_days = None
    else:
        start_date = selected_date
        end_date = selected_date
        prev_date = selected_date - timedelta(days=1)
        next_date = selected_date + timedelta(days=1)
        week_days = None

    show_completed = str(request.args.get("show_completed", "0")).lower() in ("1", "true", "on", "yes")
    recurring_due_join, effective_due_expr = recurring_effective_due_sql("t")

    week_list_rows = []

    # Pendientes por due_date dentro de rango
    pending_rows = q(
        f"SELECT t.id, t.title, t.location, t.notes, {effective_due_expr} AS due_date, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.id AS folder_id, fd.name AS folder_name "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        f"{recurring_due_join}"
        "LEFT JOIN folders fd ON fd.id=COALESCE(t.folder_id, p.folder_id) "
        f"WHERE {effective_due_expr} IS NOT NULL "
        f"AND {effective_due_expr} >= %s "
        f"AND {effective_due_expr} <= %s "
        "AND t.completed_at IS NULL "
        "AND t.deleted_at IS NULL "
        "AND (t.project_id IS NULL OR p.archived = 0) "
        f"ORDER BY {effective_due_expr} ASC, t.id DESC",
        (start_date, end_date),
    )

    # Completadas según completed_at (independiente due_date)
    completed_rows = q(
        "SELECT t.id, t.title, t.location, t.notes, t.due_date, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.id AS folder_id, fd.name AS folder_name "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=COALESCE(t.folder_id, p.folder_id) "
        "WHERE t.completed_at IS NOT NULL "
        "AND t.deleted_at IS NULL "
        "AND DATE(t.completed_at) >= %s "
        "AND DATE(t.completed_at) <= %s "
        "ORDER BY t.completed_at DESC, t.id DESC",
        (start_date, end_date),
    )

    pending_counts = {}
    for r in pending_rows:
        d = r.get("due_date")
        if d:
            key = d.isoformat()
            pending_counts[key] = pending_counts.get(key, 0) + 1

    completed_counts = {}
    for r in completed_rows:
        d = r.get("completed_at")
        if d:
            key = d.date().isoformat()
            completed_counts[key] = completed_counts.get(key, 0) + 1

    selected_day_pending = [t for t in pending_rows if t.get("due_date") == selected_date] if view == "day" else []
    selected_day_completed = [t for t in completed_rows if t.get("completed_at") and t.get("completed_at").date() == selected_date] if view == "day" else []
    next7_rows = pending_rows if view == "next7" else []

    selected_day_tasks = list(selected_day_pending)
    if show_completed:
        pending_ids = {t["id"] for t in selected_day_pending}
        for t in selected_day_completed:
            if t["id"] not in pending_ids:
                selected_day_tasks.append(t)

    tags_map = load_tags_map([t["id"] for t in selected_day_tasks]) if selected_day_tasks else {}
    has_done = len(selected_day_completed)

    if view == "next7":
        next7_task_ids = [t["id"] for t in next7_rows]
        tags_map = load_tags_map(next7_task_ids) if next7_task_ids else {}

    if view == "week":
        if show_completed:
            week_list_rows = q(
                "SELECT * FROM ("
                f"SELECT t.id, t.title, t.location, t.notes, {effective_due_expr} AS due_date, t.completed_at, t.recurrence_rule, t.priority, "
                "p.name AS project_name, p.id AS project_id, "
                "fd.name AS folder_name, fd.id AS folder_id "
                "FROM tasks t "
                "LEFT JOIN projects p ON p.id=t.project_id "
                f"{recurring_due_join}"
                "LEFT JOIN folders fd ON fd.id = COALESCE(t.folder_id, p.folder_id) "
                f"WHERE {effective_due_expr} IS NOT NULL "
                f"AND {effective_due_expr} >= %s "
                f"AND {effective_due_expr} <= %s "
                "AND t.completed_at IS NULL "
                "AND t.deleted_at IS NULL "
                "AND (t.project_id IS NULL OR p.archived = 0) "
                "UNION ALL "
                "SELECT t.id, t.title, t.location, t.notes, COALESCE(t.due_date, DATE(t.completed_at)) AS due_date, "
                "t.completed_at, t.recurrence_rule, t.priority, "
                "p.name AS project_name, p.id AS project_id, "
                "fd.name AS folder_name, fd.id AS folder_id "
                "FROM tasks t "
                "LEFT JOIN projects p ON p.id=t.project_id "
                "LEFT JOIN folders fd ON fd.id = COALESCE(t.folder_id, p.folder_id) "
                "WHERE t.completed_at IS NOT NULL "
                "AND DATE(t.completed_at) >= %s "
                "AND DATE(t.completed_at) <= %s "
                "AND t.deleted_at IS NULL "
                ") AS week_rows "
                "ORDER BY (completed_at IS NOT NULL) ASC, due_date ASC, id DESC",
                (start_date, end_date, start_date, end_date),
            )
        else:
            week_list_rows = q(
                f"SELECT t.id, t.title, t.location, t.notes, {effective_due_expr} AS due_date, t.completed_at, t.recurrence_rule, t.priority, "
                "p.name AS project_name, p.id AS project_id, "
                "fd.name AS folder_name, fd.id AS folder_id "
                "FROM tasks t "
                "LEFT JOIN projects p ON p.id=t.project_id "
                f"{recurring_due_join}"
                "LEFT JOIN folders fd ON fd.id = COALESCE(t.folder_id, p.folder_id) "
                f"WHERE {effective_due_expr} IS NOT NULL "
                f"AND {effective_due_expr} >= %s "
                f"AND {effective_due_expr} <= %s "
                "AND t.completed_at IS NULL "
                "AND t.deleted_at IS NULL "
                "AND (t.project_id IS NULL OR p.archived = 0) "
                f"ORDER BY {effective_due_expr} ASC, t.id DESC",
                (start_date, end_date),
            )
        week_task_ids = [t["id"] for t in week_list_rows]
        tags_map = load_tags_map(week_task_ids) if week_task_ids else {}

    month_weeks = _build_month_weeks(selected_date.year, selected_date.month)

    return render_template(
        "calendar.html",
        view=view,
        selected_date=selected_date,
        today_date=today_date,
        start_date=start_date,
        end_date=end_date,
        prev_date=prev_date,
        next_date=next_date,
        month_weeks=month_weeks,
        week_days=week_days,
        pending_counts=pending_counts,
        completed_counts=completed_counts,
        show_completed=show_completed,
        selected_day_tasks=selected_day_tasks,
        selected_day_pending=selected_day_pending,
        selected_day_completed=selected_day_completed,
        next7_rows=next7_rows,
        week_list_rows=week_list_rows,
        has_done=has_done,
        tags_map=tags_map,
    )


@app.route("/next")
def next_actions():
    per_page = cfg_int(["app", "pagination", "nextactions_per_page"], default=25, min_v=5, max_v=500)
    recurring_due_join, effective_due_expr = recurring_effective_due_sql("t")

    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    page = max(page, 1)

    tag = q1("SELECT id FROM tags WHERE name=%s", (role_tag("next_action"),))

    if not tag:
        rows = []
        total = 0
        pages = 1
    else:
        total_row = q1(
            "SELECT COUNT(*) AS c "
            "FROM tasks t "
            "JOIN task_tags tt ON tt.task_id=t.id "
            "LEFT JOIN projects p ON p.id=t.project_id "
            "WHERE tt.tag_id=%s "
            "AND t.completed_at IS NULL "
            "AND t.deleted_at IS NULL "
            "AND (t.project_id IS NULL OR p.archived = 0)",
            (tag["id"],)
        )
        total = int(total_row["c"]) if total_row else 0
        pages = max(1, (total + per_page - 1) // per_page)

        if page > pages:
            page = pages

        offset = (page - 1) * per_page

        rows = q(
            f"SELECT t.id, t.title, t.location, t.notes, {effective_due_expr} AS due_date, t.due_time, t.recurrence_rule, t.completed_at, t.priority, "
            "p.name AS project_name, p.id AS project_id, "
            "fd.name AS folder_name, fd.id AS folder_id "
            "FROM tasks t "
            "JOIN task_tags tt ON tt.task_id=t.id "
            "LEFT JOIN projects p ON p.id=t.project_id "
            f"{recurring_due_join}"
            "LEFT JOIN folders fd ON fd.id = COALESCE(t.folder_id, p.folder_id) "
            "WHERE tt.tag_id=%s "
            "AND t.completed_at IS NULL "
            "AND t.deleted_at IS NULL "
            "AND (t.project_id IS NULL OR p.archived = 0) "
            f"ORDER BY ({effective_due_expr} IS NULL) ASC, {effective_due_expr} ASC, t.id DESC "
            "LIMIT %s OFFSET %s",
            (tag["id"], per_page, offset)
        )

    task_ids = [r["id"] for r in rows]
    tags_map = load_tags_map(task_ids) if task_ids else {}
    sub_counts = load_subtask_counts(subdb, task_ids)
    sub_map = load_subtasks_map(subdb, task_ids)

    return render_template(
        "next.html",
        rows=rows,
        tags_map=tags_map,
        sub_counts=sub_counts,
        sub_map=sub_map,
        page=page,
        pages=pages,
        total=total,
        per_page=per_page,
    )
