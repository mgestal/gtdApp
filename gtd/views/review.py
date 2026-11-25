"""Revisión semanal GTD."""
from __future__ import annotations

from typing import Any, Tuple

from flask import flash, jsonify, redirect, render_template, request, url_for

from gtd.core import app
from gtd.db import commit, q, rollback
from gtd.roles import (
    get_folder_tree_ids,
    role_folder_exists,
    role_folder_row,
    role_folder_tree_ids,
    role_tag_exists,
    role_tag_filter,
)
from gtd.recurrence import recurring_effective_due_sql
from gtd.common import load_tags_map
from gtd.weekly_review import (
    BLOCK_KEYS,
    finish_review,
    load_state,
    projects_without_next_action,
    set_block,
)


@app.route("/review")
def review():
    recurring_due_join, effective_due_expr = recurring_effective_due_sql("t")


    # Proyectos en Seguimiento y subcarpetas
    projects_seguimiento = []
    seguimiento_ids = role_folder_tree_ids("following")
    if seguimiento_ids:
        placeholders = ",".join(["%s"] * len(seguimiento_ids))
        projects_seguimiento = q(
            f"SELECT p.id, p.name, p.description, p.archived, f.name AS folder_name "
            f"FROM projects p "
            f"LEFT JOIN folders f ON f.id=p.folder_id "
            f"WHERE p.archived=0 AND p.deleted_at IS NULL AND p.folder_id IN ({placeholders}) "
            f"ORDER BY p.name",
            tuple(seguimiento_ids),
        ) or []

    # 1) Inbox
    inbox = q(
        f"SELECT t.id, t.title, t.location, t.notes, {effective_due_expr} AS due_date, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        f"{recurring_due_join}"
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "WHERE t.completed_at IS NULL "
        "AND t.archived = 0 "
        "AND t.deleted_at IS NULL "
        "AND t.project_id IS NULL "
        "AND t.folder_id IS NULL "
        "ORDER BY t.id DESC"
    ) or []

    inbox_projects = q(
        "SELECT p.id, p.name, p.description, p.archived, f.name AS folder_name, f.id AS folder_id "
        "FROM projects p "
        "LEFT JOIN folders f ON f.id=p.folder_id "
        "WHERE p.archived=0 "
        "AND p.deleted_at IS NULL "
        "AND p.folder_id IS NULL "
        "ORDER BY p.name"
    ) or []

    # 2) NextActions
    nextaction_filter, nextaction_params = role_tag_filter("next_action")
    nextaction_exists = role_tag_exists("next_action")

    sometime_review_folder_ids = role_folder_tree_ids("someday")

    nextaction_folder_clause = ""
    nextaction_folder_params: Tuple[Any, ...] = ()
    if sometime_review_folder_ids:
        placeholders = ",".join(["%s"] * len(sometime_review_folder_ids))
        nextaction_folder_clause = f"AND (t.folder_id IS NULL OR t.folder_id NOT IN ({placeholders})) "
        nextaction_folder_params = tuple(sorted(sometime_review_folder_ids))

    nextactions_open = q(
        f"SELECT t.id, t.title, t.location, t.notes, {effective_due_expr} AS due_date, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        f"{recurring_due_join}"
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "WHERE t.completed_at IS NULL "
        "AND t.deleted_at IS NULL "
        + nextaction_filter +
        f"AND ({effective_due_expr} IS NULL OR {effective_due_expr} >= CURDATE()) "
        + nextaction_folder_clause +
        "AND (t.project_id IS NULL OR p.archived = 0) "
        f"ORDER BY ({effective_due_expr} IS NULL) ASC, {effective_due_expr} ASC, t.id DESC",
        nextaction_params + nextaction_folder_params
    ) if nextaction_exists else []

    nextactions_overdue = q(
        f"SELECT t.id, t.title, t.location, t.notes, {effective_due_expr} AS due_date, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        f"{recurring_due_join}"
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "WHERE t.completed_at IS NULL "
        "AND t.deleted_at IS NULL "
        + nextaction_filter +
        f"AND {effective_due_expr} IS NOT NULL "
        f"AND {effective_due_expr} < CURDATE() "
        + nextaction_folder_clause +
        "AND (t.project_id IS NULL OR p.archived = 0) "
        f"ORDER BY {effective_due_expr} ASC, t.id DESC",
        nextaction_params + nextaction_folder_params
    ) if nextaction_exists else []

    # 3) Agenda futura
    en_seguimiento_filter, en_seguimiento_params = role_tag_filter("following")
    agenda_filter, agenda_params = role_tag_filter("agenda")
    en_seguimiento_exists = role_tag_exists("following")
    agenda_exists = role_tag_exists("agenda")

    upcoming_7 = q(
        f"SELECT t.id, t.title, t.location, t.notes, {effective_due_expr} AS due_date, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        f"{recurring_due_join}"
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "WHERE t.completed_at IS NULL "
        "AND t.deleted_at IS NULL "
        + agenda_filter +
        f"AND {effective_due_expr} IS NOT NULL "
        f"AND {effective_due_expr} >= CURDATE() "
        f"AND {effective_due_expr} <= DATE_ADD(CURDATE(), INTERVAL 7 DAY) "
        "AND (t.project_id IS NULL OR p.archived = 0) "
        f"ORDER BY {effective_due_expr} ASC, t.id DESC",
        agenda_params
    ) if agenda_exists else []

    en_seguimiento_now = q(
        f"SELECT t.id, t.title, t.location, t.notes, {effective_due_expr} AS due_date, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        f"{recurring_due_join}"
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "WHERE t.completed_at IS NULL "
        "AND t.deleted_at IS NULL "
        + en_seguimiento_filter +
        f"AND {effective_due_expr} IS NOT NULL "
        f"AND {effective_due_expr} <= CURDATE() "
        "AND (t.project_id IS NULL OR p.archived = 0) "
        f"ORDER BY ({effective_due_expr} IS NULL) ASC, {effective_due_expr} ASC, t.id DESC",
        en_seguimiento_params
    ) if en_seguimiento_exists else []

    en_seguimiento_next_15 = q(
        f"SELECT t.id, t.title, t.location, t.notes, {effective_due_expr} AS due_date, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        f"{recurring_due_join}"
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "WHERE t.completed_at IS NULL "
        "AND t.deleted_at IS NULL "
        + en_seguimiento_filter +
        f"AND {effective_due_expr} IS NOT NULL "
        f"AND {effective_due_expr} > CURDATE() "
        f"AND {effective_due_expr} <= DATE_ADD(CURDATE(), INTERVAL 15 DAY) "
        "AND (t.project_id IS NULL OR p.archived = 0) "
        f"ORDER BY {effective_due_expr} ASC, t.id DESC",
        en_seguimiento_params
    ) if en_seguimiento_exists else []

    # 4) Agenda pasada


    agenda_overdue = q(
        f"SELECT t.id, t.title, {effective_due_expr} AS due_date, t.location, t.notes, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        f"{recurring_due_join}"
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "WHERE t.completed_at IS NULL "
        "AND t.deleted_at IS NULL "
        + agenda_filter +
        f"AND {effective_due_expr} IS NOT NULL "
        f"AND {effective_due_expr} < CURDATE() "
        "AND (t.project_id IS NULL OR p.archived = 0) "
        f"ORDER BY {effective_due_expr} ASC, t.id DESC",
        agenda_params
    ) if agenda_exists else []

    # 5) EnEspera
    en_espera_filter, en_espera_params = role_tag_filter("waiting")
    en_espera_exists = role_tag_exists("waiting")
    en_espera_folder_row = role_folder_row("waiting")
    en_espera_folder_exists = en_espera_folder_row is not None

    # Excluir tareas de proyectos en Seguimiento o subcarpetas (seguimiento_ids calculado arriba)
    seguimiento_clause = ""
    seguimiento_params = ()
    if seguimiento_ids:
        placeholders = ",".join(["%s"] * len(seguimiento_ids))
        seguimiento_clause = (
            f"AND (t.folder_id IS NULL OR t.folder_id NOT IN ({placeholders})) "
            f"AND (p.folder_id IS NULL OR p.folder_id NOT IN ({placeholders})) "
        )
        seguimiento_params = tuple(seguimiento_ids) + tuple(seguimiento_ids)
    en_espera_tasks = q(
        f"SELECT t.id, t.title, {effective_due_expr} AS due_date, t.location, t.notes, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        f"{recurring_due_join}"
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "WHERE t.completed_at IS NULL "
        "AND t.deleted_at IS NULL "
        + en_espera_filter
        + seguimiento_clause +
        "AND (t.project_id IS NULL OR p.archived = 0) "
        f"ORDER BY ({effective_due_expr} IS NULL) ASC, {effective_due_expr} ASC, t.id DESC",
        en_espera_params + seguimiento_params
    ) if en_espera_exists else []

    en_espera_projects = []
    if en_espera_folder_exists:
        en_espera_folder = en_espera_folder_row
        if en_espera_folder and en_espera_folder.get("id") is not None:
            en_espera_projects = q(
                "SELECT p.id, p.name, p.description, p.archived, f.name AS folder_name "
                "FROM projects p "
                "LEFT JOIN folders f ON f.id=p.folder_id "
                "WHERE p.archived=0 "
                "AND p.deleted_at IS NULL "
                "AND p.folder_id=%s "
                "ORDER BY p.name",
                (en_espera_folder["id"],),
            ) or []

    # 6) Proyectos (excluyendo Sometime y sus subcarpetas)

    excluded_folder_ids = set()
    for excluded_role in ("someday", "agenda", "waiting", "following"):
        excluded_folder_ids.update(role_folder_tree_ids(excluded_role))

    if excluded_folder_ids:
        ids_placeholder = ','.join(str(fid) for fid in excluded_folder_ids)
        active_projects = q(
            f"SELECT p.id, p.name, p.description, p.archived, p.folder_id, f.name AS folder_name "
            f"FROM projects p "
            f"LEFT JOIN folders f ON f.id=p.folder_id "
            f"WHERE p.archived=0 "
            f"AND p.deleted_at IS NULL "
            f"AND (p.folder_id IS NULL OR p.folder_id NOT IN ({ids_placeholder})) "
            f"ORDER BY p.name"
        ) or []
    else:
        active_projects = q(
            "SELECT p.id, p.name, p.description, p.archived, p.folder_id, f.name AS folder_name "
            "FROM projects p "
            "LEFT JOIN folders f ON f.id=p.folder_id "
            "WHERE p.archived=0 "
            "AND p.deleted_at IS NULL "
            "ORDER BY p.name"
        ) or []

    if excluded_folder_ids:
        ids_placeholder = ','.join(str(fid) for fid in excluded_folder_ids)
        empty_projects = q(
            f"SELECT p.id, p.name, p.description, p.archived, f.name AS folder_name "
            f"FROM projects p "
            f"LEFT JOIN folders f ON f.id=p.folder_id "
            f"LEFT JOIN tasks t ON t.project_id = p.id AND t.completed_at IS NULL AND t.archived=0 AND t.deleted_at IS NULL "
            f"WHERE p.archived = 0 "
            f"AND p.deleted_at IS NULL "
            f"AND (p.folder_id IS NULL OR p.folder_id NOT IN ({ids_placeholder})) "
            f"GROUP BY p.id, p.name, p.description, p.archived, f.name "
            f"HAVING COUNT(t.id) = 0 "
            f"ORDER BY p.name"
        ) or []
    else:
        empty_projects = q(
            "SELECT p.id, p.name, p.description, p.archived, f.name AS folder_name "
            "FROM projects p "
            "LEFT JOIN folders f ON f.id=p.folder_id "
            "LEFT JOIN tasks t ON t.project_id = p.id AND t.completed_at IS NULL AND t.archived=0 AND t.deleted_at IS NULL "
            "WHERE p.archived = 0 "
            "AND p.deleted_at IS NULL "
            "GROUP BY p.id, p.name, p.description, p.archived, f.name "
            "HAVING COUNT(t.id) = 0 "
            "ORDER BY p.name"
        ) or []

    rutinas_root = role_folder_row("routines")
    rutinas_folder_ids = set()
    if rutinas_root and rutinas_root.get("id") is not None:
        rutinas_folder_ids = get_folder_tree_ids(int(rutinas_root["id"]))

    active_projects = [p for p in active_projects if p.get("folder_id") is not None]

    active_projects_rutinas = [
        p for p in active_projects
        if p.get("folder_id") is not None and int(p.get("folder_id")) in rutinas_folder_ids
    ]
    active_projects_other = [
        p for p in active_projects
        if p.get("folder_id") is not None and int(p.get("folder_id")) not in rutinas_folder_ids
    ]

    empty_projects = [p for p in empty_projects if p.get("folder_name")]

    # 7) ADTV / SomeTime
    sometime_folder_exists = role_folder_exists("someday")
    adtv_folder_exists = role_folder_exists("adtv")
    esta_semana_no_folder_exists = role_folder_exists("not_this_week")

    sometime_tasks_no_project = []
    if sometime_folder_exists:
        sometime_ids = role_folder_tree_ids("someday")
        if sometime_ids:
            placeholders = ",".join(["%s"] * len(sometime_ids))
            sometime_tasks_no_project = q(
                f"SELECT t.id, t.title, t.location, t.notes, {effective_due_expr} AS due_date, t.completed_at, t.recurrence_rule, t.priority, "
                "f.name AS folder_name, f.id AS folder_id "
                "FROM tasks t "
                f"{recurring_due_join}"
                "LEFT JOIN folders f ON f.id=t.folder_id "
                f"WHERE t.completed_at IS NULL AND t.deleted_at IS NULL AND t.project_id IS NULL AND t.folder_id IN ({placeholders}) "
                f"ORDER BY ({effective_due_expr} IS NULL) ASC, {effective_due_expr} ASC, t.id DESC",
                tuple(sometime_ids),
            ) or []

    adtv_projects = q(
        "SELECT p.id, p.name, p.description, p.archived, f.name AS folder_name "
        "FROM projects p "
        "JOIN folders f ON f.id=p.folder_id "
        "WHERE p.archived=0 AND p.deleted_at IS NULL AND f.id=%s "
        "ORDER BY p.name",
        (role_folder_row("adtv")["id"],)
    ) if adtv_folder_exists else []

    esta_semana_no_projects = q(
        "SELECT p.id, p.name, p.description, p.archived, f.name AS folder_name "
        "FROM projects p "
        "JOIN folders f ON f.id=p.folder_id "
        "WHERE p.archived=0 AND p.deleted_at IS NULL AND f.id=%s "
        "ORDER BY p.name",
        (role_folder_row("not_this_week")["id"],)
    ) if esta_semana_no_folder_exists else []

    # 8) Checklists
    checklists_folder = role_folder_row("checklists")
    checklists_folder_exists = checklists_folder is not None
    checklist_tasks = []
    checklist_projects = []

    if checklists_folder_exists:
        if checklists_folder:
            checklists_folder_id = checklists_folder['id']
            # Tareas directas en la carpeta (sin proyecto)
            checklist_tasks = q(
                f"SELECT t.id, t.title, t.location, t.notes, {effective_due_expr} AS due_date, t.completed_at, t.recurrence_rule, t.priority, "
                "p.name AS project_name, p.id AS project_id, "
                "f.name AS folder_name, f.id AS folder_id "
                "FROM tasks t "
                "LEFT JOIN projects p ON p.id=t.project_id "
                f"{recurring_due_join}"
                "LEFT JOIN folders f ON f.id=t.folder_id "
                "WHERE t.completed_at IS NULL "
                "AND t.deleted_at IS NULL "
                "AND t.folder_id=%s "
                "AND t.project_id IS NULL "
                "ORDER BY t.id DESC",
                (checklists_folder_id,)
            ) or []
            # Proyectos en la carpeta
            checklist_projects = q(
                "SELECT p.id, p.name, p.description, p.archived, f.name AS folder_name "
                "FROM projects p "
                "LEFT JOIN folders f ON f.id=p.folder_id "
                "WHERE p.archived=0 "
                "AND p.deleted_at IS NULL "
                "AND p.folder_id=%s "
                "ORDER BY p.name",
                (checklists_folder_id,)
            ) or []

    # Tags map de todas las listas de tareas
    all_task_ids = []
    for group in (
        inbox,
        nextactions_open,
        nextactions_overdue,
        upcoming_7,
        en_seguimiento_now,
        en_seguimiento_next_15,
        agenda_overdue,
        en_espera_tasks,
        sometime_tasks_no_project,
        checklist_tasks,
    ):
        all_task_ids.extend([t["id"] for t in group])

    tags_map = load_tags_map(all_task_ids) if all_task_ids else {}

    return render_template(
        "review.html",
        inbox=inbox,
        inbox_projects=inbox_projects,
        nextaction_exists=nextaction_exists,
        nextactions_open=nextactions_open,
        nextactions_overdue=nextactions_overdue,
        en_seguimiento_exists=en_seguimiento_exists,
        upcoming_7=upcoming_7,
        en_seguimiento_now=en_seguimiento_now,
        en_seguimiento_next_15=en_seguimiento_next_15,
        agenda_exists=agenda_exists,
        agenda_overdue=agenda_overdue,
        en_espera_exists=en_espera_exists,
        en_espera_tasks=en_espera_tasks,
        en_espera_folder_exists=en_espera_folder_exists,
        en_espera_projects=en_espera_projects,
        active_projects=active_projects,
        active_projects_rutinas=active_projects_rutinas,
        active_projects_other=active_projects_other,
        empty_projects=empty_projects,
        sometime_folder_exists=sometime_folder_exists,
        sometime_tasks_no_project=sometime_tasks_no_project,
        adtv_folder_exists=adtv_folder_exists,
        adtv_projects=adtv_projects,
        esta_semana_no_folder_exists=esta_semana_no_folder_exists,
        esta_semana_no_projects=esta_semana_no_projects,
        checklists_folder_exists=checklists_folder_exists,
        checklist_tasks=checklist_tasks,
        checklist_projects=checklist_projects,
        tags_map=tags_map,
        projects_seguimiento=projects_seguimiento,
        review_state=load_state(),
        projects_without_next_action=projects_without_next_action(),
    )


@app.route("/review/blocks/<key>", methods=["POST"])
def review_block_toggle(key: str):
    """Marca/desmarca un bloque de la checklist (empieza la revisión si no hay una en curso)."""
    if key not in BLOCK_KEYS:
        return jsonify({"ok": False, "error": "Bloque de revisión desconocido."}), 404
    payload = request.get_json(silent=True) or {}
    try:
        checked = set_block(key, bool(payload.get("checked")))
        commit()
    except Exception as exc:
        rollback()
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, "checked": checked, "total": len(BLOCK_KEYS)})


@app.route("/review/finish", methods=["POST"])
def review_finish():
    try:
        finished = finish_review()
        commit()
    except Exception as exc:
        rollback()
        flash(f"No se pudo terminar la revisión: {exc}", "error")
        return redirect(url_for("review"))
    if finished is None:
        flash("No hay ninguna revisión en curso.", "error")
    else:
        flash("Revisión semanal terminada.", "ok")
    return redirect(url_for("review"))
