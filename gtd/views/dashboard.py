"""Dashboard de KPIs y sus listados."""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, List, Tuple

from flask import render_template, request

from gtd.config import cfg_int
from gtd.utils import _today_madrid, coerce_int
from gtd.core import app
from gtd.db import q, q1
from gtd.roles import non_active_project_folder_ids
from gtd.recurrence import recurring_effective_due_sql
from gtd.common import load_tags_map


@app.route("/dashboard")
def dashboard():
    today = _today_madrid()
    period_days = coerce_int(request.args.get("days", 30), default=30, min_v=7, max_v=180)
    period_start = today - timedelta(days=period_days - 1)
    # Lunes de la semana actual
    monday = today - timedelta(days=today.weekday())
    # Primer día del mes actual
    first_of_month = today.replace(day=1)
    recurring_due_join, effective_due_expr = recurring_effective_due_sql("t")

    # Criterio de métricas:
    # - ACTIVAS (backlog actual): excluyen archivadas y papelera.
    # - HISTORICAS (productividad): excluyen papelera, pero pueden incluir archivadas.
    total = q1(
        "SELECT COUNT(*) AS c "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE t.deleted_at IS NULL "
        "AND t.archived=0 "
        "AND (t.project_id IS NULL OR p.archived=0)"
    )["c"]
    open_tasks = q1(
        "SELECT COUNT(*) AS c "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE t.completed_at IS NULL "
        "AND t.deleted_at IS NULL "
        "AND t.archived=0 "
        "AND (t.project_id IS NULL OR p.archived=0)"
    )["c"]
    completed = q1("SELECT COUNT(*) AS c FROM tasks WHERE completed_at IS NOT NULL AND deleted_at IS NULL")["c"]
    inbox = q1(
        "SELECT COUNT(*) AS c "
        "FROM tasks "
        "WHERE project_id IS NULL "
        "AND completed_at IS NULL "
        "AND archived=0 "
        "AND deleted_at IS NULL"
    )["c"]
    trash_tasks = q1("SELECT COUNT(*) AS c FROM tasks WHERE deleted_at IS NOT NULL")["c"]
    archived_tasks_cnt = q1("SELECT COUNT(*) AS c FROM tasks WHERE archived=1 AND deleted_at IS NULL")["c"]

    # Proyectos activos excluyendo las carpetas Rutinas/Agenda/Sometime/ADTV/Seguimiento (roles GTD) y sus subcarpetas.
    excluded_folder_ids: set[int] = non_active_project_folder_ids()

    if excluded_folder_ids:
        placeholders = ",".join(["%s"] * len(excluded_folder_ids))
        projects_cnt = q1(
            "SELECT COUNT(*) AS c "
            "FROM projects "
            "WHERE archived=0 AND deleted_at IS NULL "
            f"AND (folder_id IS NULL OR folder_id NOT IN ({placeholders}))",
            tuple(sorted(excluded_folder_ids)),
        )["c"]
    else:
        projects_cnt = q1(
            "SELECT COUNT(*) AS c FROM projects WHERE archived=0 AND deleted_at IS NULL"
        )["c"]

    archived_cnt = q1("SELECT COUNT(*) AS c FROM projects WHERE archived=1 AND deleted_at IS NULL")["c"]
    if excluded_folder_ids:
        placeholders = ",".join(["%s"] * len(excluded_folder_ids))
        pending_active = q1(
            "SELECT COUNT(*) AS c "
            "FROM tasks t "
            "LEFT JOIN projects p ON p.id=t.project_id "
            "WHERE t.completed_at IS NULL "
            "AND t.archived=0 "
            "AND t.deleted_at IS NULL "
            "AND (t.project_id IS NULL OR p.archived=0) "
            f"AND (t.folder_id IS NULL OR t.folder_id NOT IN ({placeholders})) "
            f"AND (t.project_id IS NULL OR p.folder_id IS NULL OR p.folder_id NOT IN ({placeholders}))",
            tuple(sorted(excluded_folder_ids)) + tuple(sorted(excluded_folder_ids)),
        )["c"]
    else:
        pending_active = q1(
            "SELECT COUNT(*) AS c "
            "FROM tasks t "
            "LEFT JOIN projects p ON p.id=t.project_id "
            "WHERE t.completed_at IS NULL "
            "AND t.archived=0 "
            "AND t.deleted_at IS NULL "
            "AND (t.project_id IS NULL OR p.archived=0)"
        )["c"]

    # --- ESTADÍSTICAS HISTORICAS DE COMPLETADOS ---
    comp_today = q1(
        "SELECT COUNT(*) AS c FROM ("
        "SELECT t.id FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE t.completed_at IS NOT NULL "
        "AND t.deleted_at IS NULL "
        "AND DATE(t.completed_at)=%s "
        "UNION ALL "
        "SELECT t.id FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE t.last_completed_at IS NOT NULL "
        "AND t.recurrence_rule IS NOT NULL AND TRIM(t.recurrence_rule)<>'' "
        "AND t.deleted_at IS NULL "
        "AND DATE(t.last_completed_at)=%s"
        ") AS completed_union",
        (today, today),
    )["c"]
    comp_week = q1(
        "SELECT COUNT(*) AS c FROM ("
        "SELECT t.id FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE t.completed_at IS NOT NULL "
        "AND t.deleted_at IS NULL "
        "AND DATE(t.completed_at)>=%s "
        "UNION ALL "
        "SELECT t.id FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE t.last_completed_at IS NOT NULL "
        "AND t.recurrence_rule IS NOT NULL AND TRIM(t.recurrence_rule)<>'' "
        "AND t.deleted_at IS NULL "
        "AND DATE(t.last_completed_at)>=%s"
        ") AS completed_union",
        (monday, monday),
    )["c"]
    comp_month = q1(
        "SELECT COUNT(*) AS c FROM ("
        "SELECT t.id FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE t.completed_at IS NOT NULL "
        "AND t.deleted_at IS NULL "
        "AND DATE(t.completed_at)>=%s "
        "UNION ALL "
        "SELECT t.id FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE t.last_completed_at IS NOT NULL "
        "AND t.recurrence_rule IS NOT NULL AND TRIM(t.recurrence_rule)<>'' "
        "AND t.deleted_at IS NULL "
        "AND DATE(t.last_completed_at)>=%s"
        ") AS completed_union",
        (first_of_month, first_of_month),
    )["c"]
    comp_period = q1(
        "SELECT COUNT(*) AS c FROM ("
        "SELECT t.id FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE t.completed_at IS NOT NULL "
        "AND t.deleted_at IS NULL "
        "AND DATE(t.completed_at)>=%s "
        "UNION ALL "
        "SELECT t.id FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE t.last_completed_at IS NOT NULL "
        "AND t.recurrence_rule IS NOT NULL AND TRIM(t.recurrence_rule)<>'' "
        "AND t.deleted_at IS NULL "
        "AND DATE(t.last_completed_at)>=%s"
        ") AS completed_union",
        (period_start, period_start),
    )["c"]
    created_period = q1(
        "SELECT COUNT(*) AS c FROM tasks WHERE deleted_at IS NULL AND DATE(created_at) >= %s",
        (period_start,),
    )["c"]
    close_rate = int(round((comp_period / created_period) * 100)) if created_period else 0

    focus_projects_today = q1(
        "SELECT COUNT(DISTINCT COALESCE(t.project_id, 0)) AS c "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE t.deleted_at IS NULL "
        "AND ((t.completed_at IS NOT NULL AND DATE(t.completed_at)=%s) "
        "OR (t.last_completed_at IS NOT NULL "
        "AND t.recurrence_rule IS NOT NULL AND TRIM(t.recurrence_rule)<>'' "
        "AND DATE(t.last_completed_at)=%s))",
        (today, today),
    )["c"]

    pending_age_row = q1(
        "SELECT AVG(DATEDIFF(%s, DATE(t.created_at))) AS avg_days, "
        "MAX(DATEDIFF(%s, DATE(t.created_at))) AS max_days "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE t.completed_at IS NULL "
        "AND t.archived=0 "
        "AND t.deleted_at IS NULL "
        "AND (t.project_id IS NULL OR p.archived=0)",
        (today, today),
    )
    pending_age_avg = int(round(float(pending_age_row["avg_days"]))) if pending_age_row and pending_age_row["avg_days"] is not None else 0
    pending_age_oldest = int(pending_age_row["max_days"]) if pending_age_row and pending_age_row["max_days"] is not None else 0

    habits_today_row = q1(
        f"SELECT "
        "(SELECT COUNT(*) "
        " FROM tasks t "
        " LEFT JOIN projects p ON p.id=t.project_id "
        f" {recurring_due_join}"
        " WHERE t.recurrence_rule IS NOT NULL "
        " AND TRIM(t.recurrence_rule)<>'' "
        " AND t.archived=0 "
        " AND t.deleted_at IS NULL "
        " AND (t.project_id IS NULL OR p.archived=0) "
        f" AND {effective_due_expr}=%s) AS due_today, "
        "(SELECT COUNT(*) "
        " FROM recurring_task_runs rr "
        " JOIN tasks t ON t.id=rr.task_id "
        " LEFT JOIN projects p ON p.id=t.project_id "
        " WHERE rr.previous_due_date=%s "
        " AND DATE(rr.executed_at)=%s "
        " AND t.archived=0 "
        " AND t.deleted_at IS NULL "
        " AND (t.project_id IS NULL OR p.archived=0)) AS done_today",
        (today, today, today),
    )
    habits_due_today = int(habits_today_row["due_today"] or 0) if habits_today_row else 0
    habits_done_today = int(habits_today_row["done_today"] or 0) if habits_today_row else 0
    habits_planned_today = habits_due_today + habits_done_today
    habits_rate = int(round((habits_done_today / habits_planned_today) * 100)) if habits_planned_today else 0

    focus_split_row = q1(
        "SELECT "
        "SUM(CASE WHEN t.project_id IS NULL THEN 1 ELSE 0 END) AS reactive_cnt, "
        "SUM(CASE WHEN t.project_id IS NOT NULL THEN 1 ELSE 0 END) AS structured_cnt "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE t.completed_at IS NULL "
        "AND t.archived=0 "
        "AND t.deleted_at IS NULL "
        "AND (t.project_id IS NULL OR p.archived=0)"
    )
    reactive_active = int(focus_split_row["reactive_cnt"] or 0) if focus_split_row else 0
    structured_active = int(focus_split_row["structured_cnt"] or 0) if focus_split_row else 0
    structured_share = int(round((structured_active / (structured_active + reactive_active)) * 100)) if (structured_active + reactive_active) else 0

    avg_completion_row = q1(
        "SELECT ROUND(AVG(DATEDIFF(DATE(t.completed_at), DATE(t.created_at)))) AS avg_days "
        "FROM tasks t "
        "WHERE t.completed_at IS NOT NULL "
        "AND t.deleted_at IS NULL "
        "AND DATE(t.completed_at) >= %s",
        (period_start,),
    )
    avg_completion_days = int(avg_completion_row["avg_days"]) if avg_completion_row and avg_completion_row["avg_days"] is not None else 0

    overdue_rescue_today = int(q1(
        "SELECT COUNT(*) AS c FROM tasks "
        "WHERE completed_at IS NOT NULL "
        "AND deleted_at IS NULL "
        "AND DATE(completed_at) = %s "
        "AND due_date IS NOT NULL "
        "AND due_date < DATE(completed_at)",
        (today,),
    )["c"] or 0)
    overdue_rescue_week = int(q1(
        "SELECT COUNT(*) AS c FROM tasks "
        "WHERE completed_at IS NOT NULL "
        "AND deleted_at IS NULL "
        "AND DATE(completed_at) >= %s "
        "AND due_date IS NOT NULL "
        "AND due_date < DATE(completed_at)",
        (monday,),
    )["c"] or 0)

    comp_struct_row = q1(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN t.project_id IS NOT NULL THEN 1 ELSE 0 END) AS with_project "
        "FROM tasks t "
        "WHERE t.completed_at IS NOT NULL "
        "AND t.deleted_at IS NULL "
        "AND DATE(t.completed_at) >= %s",
        (monday,),
    )
    comp_total_week = int(comp_struct_row["total"] or 0) if comp_struct_row else 0
    comp_with_project_cnt = int(comp_struct_row["with_project"] or 0) if comp_struct_row else 0
    comp_project_share = int(round((comp_with_project_cnt / comp_total_week) * 100)) if comp_total_week else 0

    # --- TENDENCIA DIARIA DE COMPLETADAS ---
    trend_rows = q(
        "SELECT DATE(completed_at) AS d, COUNT(*) AS c "
        "FROM tasks "
        "WHERE completed_at IS NOT NULL "
        "AND deleted_at IS NULL "
        "AND DATE(completed_at) >= %s "
        "GROUP BY DATE(completed_at) "
        "ORDER BY d ASC",
        (period_start,),
    )
    trend_map = {r["d"]: int(r["c"]) for r in trend_rows}
    trend_values: List[int] = []
    trend_labels: List[str] = []
    for i in range(period_days):
        d = period_start + timedelta(days=i)
        trend_values.append(trend_map.get(d, 0))
        trend_labels.append(d.strftime("%d/%m"))

    svg_w, svg_h = 920, 230
    pad_l, pad_r, pad_t, pad_b = 32, 8, 12, 34
    plot_w = svg_w - pad_l - pad_r
    plot_h = svg_h - pad_t - pad_b
    vmax = max(max(trend_values, default=0), 1)
    n_points = max(1, len(trend_values))

    trend_points: List[Dict[str, Any]] = []
    for idx, val in enumerate(trend_values):
        x = pad_l + (plot_w * idx / (n_points - 1 if n_points > 1 else 1))
        y = pad_t + (plot_h * (1 - (val / vmax)))
        trend_points.append({
            "x": round(x, 2),
            "y": round(y, 2),
            "value": val,
            "label": trend_labels[idx],
        })
    trend_polyline = " ".join(f"{p['x']},{p['y']}" for p in trend_points)
    y_grid = [round(pad_t + (plot_h * i / 5), 2) for i in range(6)]
    axis_y = round(pad_t + plot_h, 2)

    tick_count = 5 if period_days <= 30 else 7
    tick_indices = sorted({
        int(round(i * (n_points - 1) / (tick_count - 1)))
        for i in range(tick_count)
    })
    x_ticks = [
        {
            "x": trend_points[i]["x"],
            "label": trend_points[i]["label"],
        }
        for i in tick_indices
    ]

    # --- TOP PROYECTOS COMPLETADOS EN PERIODO ---
    completed_by_project = q(
        "SELECT p.id AS id, COALESCE(p.name, 'Inbox') AS name, COUNT(*) AS c "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE t.completed_at IS NOT NULL "
        "AND t.deleted_at IS NULL "
        "AND DATE(t.completed_at) >= %s "
        "GROUP BY p.id, COALESCE(p.name, 'Inbox') "
        "ORDER BY c DESC, name ASC "
        "LIMIT 6",
        (period_start,),
    )
    project_max = max([int(r["c"]) for r in completed_by_project], default=1)

    # --- TOP ETIQUETAS EN PERIODO ---
    top_tags = q(
        "SELECT tg.id AS id, tg.name AS name, COUNT(*) AS c "
        "FROM task_tags tt "
        "INNER JOIN tags tg ON tg.id=tt.tag_id "
        "INNER JOIN tasks t ON t.id=tt.task_id "
        "WHERE t.completed_at IS NOT NULL "
        "AND t.deleted_at IS NULL "
        "AND DATE(t.completed_at) >= %s "
        "GROUP BY tg.id, tg.name "
        "ORDER BY c DESC, tg.name ASC "
        "LIMIT 10",
        (period_start,),
    )

    # --- CONSULTA MEJORADA DE VENCIMIENTOS (CON UBICACIÓN) ---
    due_soon = q(
                f"""
                SELECT t.id, t.title, t.location, t.notes, {effective_due_expr} AS due_date, t.project_id, t.folder_id, t.priority,
               p.name AS project_name, f.name AS folder_name
        FROM tasks t
        LEFT JOIN projects p ON t.project_id = p.id
                {recurring_due_join}
        LEFT JOIN folders f ON t.folder_id = f.id
        WHERE t.completed_at IS NULL 
                    AND t.archived = 0
                    AND t.deleted_at IS NULL
                    AND {effective_due_expr} IS NOT NULL 
                    AND {effective_due_expr} <= %s 
                    AND (t.project_id IS NULL OR p.archived = 0)
                ORDER BY {effective_due_expr} ASC 
        """,
        (today + timedelta(days=7),)
    )
    due_soon_task_ids = [t["id"] for t in due_soon]
    due_soon_tags_map = load_tags_map(due_soon_task_ids) if due_soon_task_ids else {}

    return render_template(
        "dashboard.html",
        stats={
            "total": total,
            "open": open_tasks,
            "completed": completed,
            "inbox": inbox,
            "trash_tasks": trash_tasks,
            "archived_tasks": archived_tasks_cnt,
            "projects": projects_cnt,
            "archived": archived_cnt,
            "archived_projects": archived_cnt,
            "comp_today": comp_today,
            "comp_week": comp_week,
            "comp_month": comp_month,
            "comp_period": comp_period,
            "close_rate": close_rate,
            "pending_active": pending_active,
        },
        focus={
            "projects_today": int(focus_projects_today or 0),
            "pending_age_avg": pending_age_avg,
            "pending_age_oldest": pending_age_oldest,
            "habits_done_today": habits_done_today,
            "habits_planned_today": habits_planned_today,
            "habits_rate": habits_rate,
            "reactive_active": reactive_active,
            "structured_active": structured_active,
            "structured_share": structured_share,
            "avg_completion_days": avg_completion_days,
            "overdue_rescue_today": overdue_rescue_today,
            "overdue_rescue_week": overdue_rescue_week,
            "comp_project_share": comp_project_share,
            "comp_with_project": comp_with_project_cnt,
            "comp_total_week": comp_total_week,
        },
        period_days=period_days,
        trend_points=trend_points,
        trend_polyline=trend_polyline,
        y_grid=y_grid,
        axis_y=axis_y,
        x_ticks=x_ticks,
        trend_max=vmax,
        completed_by_project=completed_by_project,
        project_max=project_max,
        top_tags=top_tags,
        today_d=today,
        due_soon=due_soon,
        due_soon_tags_map=due_soon_tags_map,
    )


@app.route("/dashboard/completed")
def dashboard_completed():
    period = (request.args.get("period") or "today").strip().lower()
    if period not in ("today", "week", "month"):
        period = "today"

    per_page = cfg_int(["app", "pagination", "search_per_page"], default=25, min_v=5, max_v=500)
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    page = max(page, 1)

    today = _today_madrid()
    monday = today - timedelta(days=today.weekday())
    first_of_month = today.replace(day=1)

    if period == "today":
        where_sql = "DATE(t.completed_at) = %s"
        where_params: Tuple[Any, ...] = (today,)
        title = "Tareas realizadas hoy"
        subtitle = today.strftime("%d-%m-%Y")
    elif period == "week":
        where_sql = "DATE(t.completed_at) >= %s"
        where_params = (monday,)
        title = "Tareas realizadas esta semana"
        subtitle = f"Desde {monday.strftime('%d-%m-%Y')}"
    else:
        where_sql = "DATE(t.completed_at) >= %s"
        where_params = (first_of_month,)
        title = "Tareas realizadas este mes"
        subtitle = first_of_month.strftime("Desde %d-%m-%Y")

    total_row = q1(
        "SELECT COUNT(*) AS c FROM ("
        "SELECT t.id FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE t.completed_at IS NOT NULL "
        "AND t.deleted_at IS NULL "
        f"AND {where_sql} "
        "UNION ALL "
        "SELECT t.id FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE t.last_completed_at IS NOT NULL "
        "AND t.recurrence_rule IS NOT NULL AND TRIM(t.recurrence_rule) <> '' "
        "AND t.deleted_at IS NULL "
        f"AND {where_sql.replace('t.completed_at', 't.last_completed_at')} "
        ") AS combined_tasks",
        where_params + where_params,
    )
    total = int(total_row["c"]) if total_row else 0
    pages = max(1, (total + per_page - 1) // per_page)
    if page > pages:
        page = pages
    offset = (page - 1) * per_page

    rows = q(
        "SELECT * FROM ("
        "SELECT t.id, t.title, t.location, t.notes, t.due_date, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=COALESCE(t.folder_id, p.folder_id) "
        "WHERE t.completed_at IS NOT NULL "
        "AND t.deleted_at IS NULL "
        f"AND {where_sql} "
        "UNION ALL "
        "SELECT t.id, t.title, t.location, t.notes, t.due_date, t.last_completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=COALESCE(t.folder_id, p.folder_id) "
        "WHERE t.last_completed_at IS NOT NULL "
        "AND t.recurrence_rule IS NOT NULL AND TRIM(t.recurrence_rule) <> '' "
        "AND t.deleted_at IS NULL "
        f"AND {where_sql.replace('t.completed_at', 't.last_completed_at')} "
        ") AS completed_union "
        "ORDER BY completed_at DESC, id DESC "
        "LIMIT %s OFFSET %s",
        where_params + where_params + (per_page, offset),
    )

    task_ids = [r["id"] for r in rows]
    tags_map = load_tags_map(task_ids) if task_ids else {}

    return render_template(
        "dashboard_completed.html",
        period=period,
        title=title,
        subtitle=subtitle,
        rows=rows,
        tags_map=tags_map,
        total=total,
        page=page,
        pages=pages,
        per_page=per_page,
    )


@app.route("/dashboard/pending-active")
def dashboard_pending_active():
    per_page = cfg_int(["app", "pagination", "search_per_page"], default=25, min_v=5, max_v=500)
    recurring_due_join, effective_due_expr = recurring_effective_due_sql("t")
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    page = max(page, 1)

    # Rutinas/Agenda/Sometime/ADTV/Seguimiento (roles GTD) y sus subcarpetas.
    excluded_folder_ids: set[int] = non_active_project_folder_ids()

    base_from_sql = (
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
    )
    base_where_sql = (
        "WHERE t.completed_at IS NULL "
        "AND t.archived=0 "
        "AND t.deleted_at IS NULL "
        "AND (t.project_id IS NULL OR p.archived=0)"
    )
    filter_sql = ""
    filter_params: Tuple[Any, ...] = ()
    if excluded_folder_ids:
        placeholders = ",".join(["%s"] * len(excluded_folder_ids))
        filter_sql = (
            f" AND (t.folder_id IS NULL OR t.folder_id NOT IN ({placeholders}))"
            f" AND (t.project_id IS NULL OR p.folder_id IS NULL OR p.folder_id NOT IN ({placeholders}))"
        )
        ids_sorted = tuple(sorted(excluded_folder_ids))
        filter_params = ids_sorted + ids_sorted

    total_row = q1(
        "SELECT COUNT(*) AS c " + base_from_sql + base_where_sql + filter_sql,
        filter_params,
    )
    total = int(total_row["c"]) if total_row else 0
    pages = max(1, (total + per_page - 1) // per_page)
    if page > pages:
        page = pages
    offset = (page - 1) * per_page

    rows = q(
        f"SELECT t.id, t.title, t.location, t.notes, {effective_due_expr} AS due_date, t.due_time, t.priority, t.created_at, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        + base_from_sql
        + recurring_due_join
        + "LEFT JOIN folders fd ON fd.id=COALESCE(t.folder_id, p.folder_id) "
        + base_where_sql
        + filter_sql
        + f" ORDER BY ({effective_due_expr} IS NULL) ASC, {effective_due_expr} ASC, (t.due_time IS NULL) ASC, t.due_time ASC, t.id DESC "
        + "LIMIT %s OFFSET %s",
        filter_params + (per_page, offset),
    )

    task_ids = [r["id"] for r in rows]
    tags_map = load_tags_map(task_ids) if task_ids else {}

    return render_template(
        "dashboard_pending_active.html",
        title="Tareas pendientes activas",
        subtitle="Excluye Rutinas, Agenda, Sometime, ADTV y Seguimiento (incluye subcarpetas)",
        rows=rows,
        tags_map=tags_map,
        total=total,
        page=page,
        pages=pages,
        per_page=per_page,
    )
