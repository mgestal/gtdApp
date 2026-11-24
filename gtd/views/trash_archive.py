"""Papelera y archivo de tareas/proyectos."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from flask import flash, redirect, render_template, request, url_for

from gtd.config import BACKUP_DIR, cfg_int
from gtd.utils import (
    _archive_scope_sql,
    _d_to_str,
    _dt_to_str,
    _parse_date_or_none,
    _parse_datetime_or_none,
    _parse_time_or_none,
    normalize_name,
    safe_archive_backup_filename,
    safe_next_url,
)
from gtd.core import app
from gtd.db import commit, exec_sql, q, q1, rollback
from gtd.lookups import get_or_create_tag
from gtd.calendar_sync import _mark_task_calendar_dirty
from gtd.common import admin_required, load_tags_map
from gtd.backups import list_archive_task_backups


def _restore_task_from_trash(task_id: int) -> bool:
    row = q1("SELECT id, deleted_prev_archived FROM tasks WHERE id=%s AND deleted_at IS NOT NULL", (task_id,))
    if not row:
        return False

    prev_archived = int(row.get("deleted_prev_archived") or 0)
    archived_at = "NULL" if prev_archived == 0 else "COALESCE(archived_at, NOW())"
    exec_sql(
        "UPDATE tasks "
        f"SET deleted_at=NULL, archived=%s, archived_at={archived_at}, deleted_prev_archived=0 "
        "WHERE id=%s",
        (prev_archived, task_id),
    )
    _mark_task_calendar_dirty(task_id)
    return True


def _hard_delete_trashed_tasks(older_than_days: Optional[int] = None) -> None:
    where = "t.deleted_at IS NOT NULL"
    params: List[Any] = []
    if older_than_days is not None:
        where += " AND t.deleted_at < (NOW() - INTERVAL %s DAY)"
        params.append(int(older_than_days))

    exec_sql(
        "DELETE tt FROM task_tags tt "
        "JOIN tasks t ON t.id=tt.task_id "
        "WHERE " + where,
        tuple(params),
    )
    exec_sql(
        "DELETE st FROM subtasks st "
        "JOIN tasks t ON t.id=st.task_id "
        "WHERE " + where,
        tuple(params),
    )
    exec_sql(
        "DELETE rr FROM recurring_task_runs rr "
        "JOIN tasks t ON t.id=rr.task_id "
        "WHERE " + where,
        tuple(params),
    )
    exec_sql(
        "DELETE t FROM tasks t WHERE " + where,
        tuple(params),
    )


def _hard_delete_trashed_projects() -> None:
    exec_sql("DELETE FROM projects WHERE deleted_at IS NOT NULL")


def _restore_project_from_trash(project_id: int) -> bool:
    row = q1("SELECT id, deleted_prev_archived FROM projects WHERE id=%s AND deleted_at IS NOT NULL", (project_id,))
    if not row:
        return False

    prev_archived = int(row.get("deleted_prev_archived") or 0)
    archived_at = "NULL" if prev_archived == 0 else "COALESCE(archived_at, NOW())"
    exec_sql(
        "UPDATE projects "
        f"SET deleted_at=NULL, archived=%s, archived_at={archived_at}, deleted_prev_archived=0, updated_at=NOW() "
        "WHERE id=%s",
        (prev_archived, project_id),
    )

    # Restaurar también tareas del proyecto que estén en papelera.
    rows = q(
        "SELECT id, deleted_prev_archived FROM tasks "
        "WHERE project_id=%s AND deleted_at IS NOT NULL",
        (project_id,),
    )
    for t in rows:
        _restore_task_from_trash(int(t["id"]))

    return True


def _handle_trash_action(action: str, default_endpoint: str):
    handled = {
        "trash_purge_all",
        "trash_purge_tasks_old",
        "trash_restore_task",
        "trash_restore_project",
    }
    if action not in handled:
        return None

    next_url = safe_next_url(request.form.get("next"), default_endpoint)

    if not admin_required():
        flash("No autorizado.", "error")
        return redirect(next_url)

    if action == "trash_purge_all":
        try:
            _hard_delete_trashed_tasks(older_than_days=None)
            _hard_delete_trashed_projects()
            commit()
            flash("Papelera vaciada por completo.", "ok")
        except Exception as e:
            rollback()
            flash(f"No se pudo vaciar la papelera: {e}", "error")
        return redirect(next_url)

    if action == "trash_purge_tasks_old":
        try:
            _hard_delete_trashed_tasks(older_than_days=7)
            commit()
            flash("Tareas eliminadas hace más de una semana borradas definitivamente.", "ok")
        except Exception as e:
            rollback()
            flash(f"No se pudieron limpiar tareas antiguas de papelera: {e}", "error")
        return redirect(next_url)

    if action == "trash_restore_task":
        task_id_raw = (request.form.get("task_id") or "").strip()
        try:
            task_id = int(task_id_raw)
        except Exception:
            flash("task_id inválido.", "error")
            return redirect(next_url)

        try:
            restored = _restore_task_from_trash(task_id)
            if restored:
                commit()
                flash("Tarea restaurada desde papelera.", "ok")
            else:
                rollback()
                flash("La tarea no existe en papelera.", "error")
        except Exception as e:
            rollback()
            flash(f"No se pudo restaurar la tarea: {e}", "error")
        return redirect(next_url)

    project_id_raw = (request.form.get("project_id") or "").strip()
    try:
        project_id = int(project_id_raw)
    except Exception:
        flash("project_id inválido.", "error")
        return redirect(next_url)

    try:
        restored = _restore_project_from_trash(project_id)
        if restored:
            commit()
            flash("Proyecto (y sus tareas) restaurado desde papelera.", "ok")
        else:
            rollback()
            flash("El proyecto no existe en papelera.", "error")
    except Exception as e:
        rollback()
        flash(f"No se pudo restaurar el proyecto: {e}", "error")
    return redirect(next_url)


def _load_trash_view_data(task_page: int, project_page: int) -> Dict[str, Any]:
    task_per_page = cfg_int(["app", "pagination", "archive_tasks_per_page"], default=25, min_v=5, max_v=500)
    project_per_page = cfg_int(["app", "pagination", "archive_projects_per_page"], default=25, min_v=5, max_v=500)

    total_tasks_row = q1("SELECT COUNT(*) AS c FROM tasks WHERE deleted_at IS NOT NULL")
    total_projects_row = q1("SELECT COUNT(*) AS c FROM projects WHERE deleted_at IS NOT NULL")

    total_tasks = int(total_tasks_row["c"]) if total_tasks_row else 0
    total_projects = int(total_projects_row["c"]) if total_projects_row else 0

    task_pages = max(1, (total_tasks + task_per_page - 1) // task_per_page)
    project_pages = max(1, (total_projects + project_per_page - 1) // project_per_page)

    task_page = max(1, min(task_page, task_pages))
    project_page = max(1, min(project_page, project_pages))

    task_offset = (task_page - 1) * task_per_page
    project_offset = (project_page - 1) * project_per_page

    trashed_tasks = q(
        "SELECT t.id, t.title, t.deleted_at, p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=COALESCE(t.folder_id, p.folder_id) "
        "WHERE t.deleted_at IS NOT NULL "
        "ORDER BY t.deleted_at DESC, t.id DESC "
        "LIMIT %s OFFSET %s",
        (task_per_page, task_offset),
    )

    trashed_projects = q(
        "SELECT p.id, p.name, p.deleted_at, f.name AS folder_name, f.id AS folder_id "
        "FROM projects p "
        "LEFT JOIN folders f ON f.id=p.folder_id "
        "WHERE p.deleted_at IS NOT NULL "
        "ORDER BY p.deleted_at DESC, p.id DESC "
        "LIMIT %s OFFSET %s",
        (project_per_page, project_offset),
    )

    return {
        "trash_counts": {"tasks": total_tasks, "projects": total_projects},
        "trashed_tasks": trashed_tasks,
        "trashed_projects": trashed_projects,
        "task_page": task_page,
        "task_pages": task_pages,
        "total_tasks": total_tasks,
        "task_per_page": task_per_page,
        "project_page": project_page,
        "project_pages": project_pages,
        "total_projects": total_projects,
        "project_per_page": project_per_page,
    }


def _handle_archive_action(action: str, default_endpoint: str):
    handled = {"archive_backup_tasks", "archive_delete_tasks", "archive_import_backup"}
    if action not in handled:
        return None

    next_url = safe_next_url(request.form.get("next"), default_endpoint)

    if not admin_required():
        flash("No autorizado.", "error")
        return redirect(next_url)

    if action == "archive_backup_tasks":
        scope = (request.form.get("archive_scope") or "all").strip().lower()
        scope_sql, scope_params, scope_label = _archive_scope_sql(scope)

        try:
            rows = q(
                "SELECT t.id, t.project_id, t.folder_id, t.title, t.location, t.notes, t.due_date, t.due_time, "
                "t.created_at, t.completed_at, t.last_completed_at, t.recurrence_rule, t.archived_at "
                "FROM tasks t "
                "WHERE t.archived=1 AND t.deleted_at IS NULL"
                + scope_sql
                + " ORDER BY t.id ASC",
                scope_params,
            )

            task_ids = [int(r["id"]) for r in rows]
            tags_map: Dict[int, List[str]] = {}
            if task_ids:
                placeholders = ",".join(["%s"] * len(task_ids))
                tag_rows = q(
                    "SELECT tt.task_id, tg.name "
                    "FROM task_tags tt "
                    "JOIN tags tg ON tg.id=tt.tag_id "
                    f"WHERE tt.task_id IN ({placeholders}) "
                    "ORDER BY tt.task_id ASC, tg.name ASC",
                    tuple(task_ids),
                )
                for tr in tag_rows:
                    tid = int(tr["task_id"])
                    tags_map.setdefault(tid, []).append(str(tr.get("name") or ""))

            export_rows: List[Dict[str, Any]] = []
            for r in rows:
                export_rows.append(
                    {
                        "source_task_id": int(r["id"]),
                        "project_id": r.get("project_id"),
                        "folder_id": r.get("folder_id"),
                        "title": r.get("title"),
                        "notes": r.get("notes"),
                        "due_date": _d_to_str(r.get("due_date")),
                        "due_time": _dt_to_str(r.get("due_time")),
                        "created_at": _dt_to_str(r.get("created_at")),
                        "completed_at": _dt_to_str(r.get("completed_at")),
                        "last_completed_at": _dt_to_str(r.get("last_completed_at")),
                        "recurrence_rule": r.get("recurrence_rule"),
                        "archived_at": _dt_to_str(r.get("archived_at")),
                        "tags": tags_map.get(int(r["id"]), []),
                    }
                )

            archived_projects = q(
                "SELECT p.id, p.folder_id, p.name, p.description, p.archived_at, p.created_at, p.updated_at "
                "FROM projects p "
                "WHERE p.archived=1 AND p.deleted_at IS NULL "
                "ORDER BY p.id ASC"
            )

            export_projects: List[Dict[str, Any]] = []
            for p in archived_projects:
                export_projects.append(
                    {
                        "source_project_id": int(p["id"]),
                        "folder_id": p.get("folder_id"),
                        "name": p.get("name"),
                        "description": p.get("description"),
                        "archived_at": _dt_to_str(p.get("archived_at")),
                        "created_at": _dt_to_str(p.get("created_at")),
                        "updated_at": _dt_to_str(p.get("updated_at")),
                    }
                )

            payload = {
                "format": "gtd_archive_backup_v2",
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "scope": scope,
                "projects_count": len(export_projects),
                "projects": export_projects,
                "tasks_count": len(export_rows),
                "tasks": export_rows,
            }

            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            fname = f"archive_tasks_{ts}.json"
            path = BACKUP_DIR / fname
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

            flash(
                f"Backup de archivo creado ({scope_label}): {fname} ({len(export_rows)} tareas, {len(export_projects)} proyectos).",
                "ok",
            )
        except Exception as e:
            flash(f"No se pudo crear el backup de archivo: {e}", "error")

        return redirect(next_url)

    if action == "archive_delete_tasks":
        scope = (request.form.get("archive_scope") or "all").strip().lower()
        scope_sql, scope_params, scope_label = _archive_scope_sql(scope)

        try:
            total_row = q1(
                "SELECT COUNT(*) AS c "
                "FROM tasks t "
                "WHERE t.archived=1 AND t.deleted_at IS NULL"
                + scope_sql,
                scope_params,
            )
            total = int((total_row or {}).get("c") or 0)

            if total > 0:
                exec_sql(
                    "UPDATE tasks t "
                    "SET t.deleted_prev_archived=t.archived, t.deleted_at=NOW(), t.archived=1, t.archived_at=COALESCE(t.archived_at, NOW()) "
                    "WHERE t.archived=1 AND t.deleted_at IS NULL"
                    + scope_sql,
                    scope_params,
                )

                exec_sql(
                    "UPDATE tasks t "
                    "SET t.calendar_sync_state='pending_delete', t.calendar_local_changed_at=NOW() "
                    "WHERE t.archived=1 "
                    "AND t.deleted_at IS NOT NULL "
                    "AND t.google_event_id IS NOT NULL"
                    + scope_sql,
                    scope_params,
                )

            commit()
            flash(
                f"Tareas del archivo enviadas a papelera ({scope_label}): {total}.",
                "ok",
            )
        except Exception as e:
            rollback()
            flash(f"No se pudieron eliminar tareas del archivo: {e}", "error")

        return redirect(next_url)

    fname = safe_archive_backup_filename(request.form.get("archive_backup_file", ""))
    if not fname:
        flash("Backup de archivo inválido.", "error")
        return redirect(next_url)

    path = BACKUP_DIR / fname
    if not path.exists():
        flash("El backup de archivo seleccionado no existe.", "error")
        return redirect(next_url)

    imported = 0
    imported_projects = 0
    skipped = 0

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        projects_data = payload.get("projects", []) if isinstance(payload, dict) else []
        tasks_data = payload.get("tasks", []) if isinstance(payload, dict) else []

        if projects_data is None:
            projects_data = []
        if not isinstance(projects_data, list):
            flash("Formato de backup inválido.", "error")
            return redirect(next_url)
        if not isinstance(tasks_data, list):
            flash("Formato de backup inválido.", "error")
            return redirect(next_url)

        project_id_map: Dict[int, int] = {}

        for item in projects_data:
            if not isinstance(item, dict):
                continue

            project_name = (str(item.get("name") or "")).strip()
            if not project_name:
                continue

            source_project_id_raw = item.get("source_project_id")
            source_project_id: Optional[int] = None
            try:
                if source_project_id_raw is not None:
                    source_project_id = int(source_project_id_raw)
            except Exception:
                source_project_id = None

            folder_id = item.get("folder_id")
            valid_folder_id: Optional[int] = None
            try:
                if folder_id is not None:
                    fid = int(folder_id)
                    f = q1("SELECT id FROM folders WHERE id=%s", (fid,))
                    if f:
                        valid_folder_id = fid
            except Exception:
                valid_folder_id = None

            reused_project_id: Optional[int] = None
            if source_project_id is not None:
                existing_by_id = q1(
                    "SELECT id, name, archived FROM projects WHERE id=%s AND deleted_at IS NULL",
                    (source_project_id,),
                )
                if existing_by_id and normalize_name(existing_by_id.get("name") or "") == normalize_name(project_name) and int(existing_by_id.get("archived") or 0) == 1:
                    reused_project_id = int(existing_by_id["id"])

            if reused_project_id is None:
                if valid_folder_id is None:
                    existing_archived = q1(
                        "SELECT id FROM projects WHERE archived=1 AND deleted_at IS NULL AND name=%s AND folder_id IS NULL ORDER BY id ASC LIMIT 1",
                        (project_name,),
                    )
                else:
                    existing_archived = q1(
                        "SELECT id FROM projects WHERE archived=1 AND deleted_at IS NULL AND name=%s AND folder_id=%s ORDER BY id ASC LIMIT 1",
                        (project_name, valid_folder_id),
                    )
                if existing_archived:
                    reused_project_id = int(existing_archived["id"])

            if reused_project_id is None:
                description = item.get("description")
                archived_at = _parse_datetime_or_none(item.get("archived_at")) or datetime.now()
                created_at = _parse_datetime_or_none(item.get("created_at")) or datetime.now()
                updated_at = _parse_datetime_or_none(item.get("updated_at"))
                reused_project_id = int(
                    exec_sql(
                        "INSERT INTO projects(folder_id, name, description, archived, archived_at, created_at, updated_at) "
                        "VALUES(%s,%s,%s,1,%s,%s,%s)",
                        (valid_folder_id, project_name, description, archived_at, created_at, updated_at),
                    )
                    or 0
                )
                imported_projects += 1

            if source_project_id is not None and reused_project_id:
                project_id_map[source_project_id] = reused_project_id

        for item in tasks_data:
            if not isinstance(item, dict):
                skipped += 1
                continue

            title = (str(item.get("title") or "")).strip()
            if not title:
                skipped += 1
                continue

            project_id = item.get("project_id")
            folder_id = item.get("folder_id")

            valid_project_id: Optional[int] = None
            valid_folder_id: Optional[int] = None

            try:
                if project_id is not None:
                    pid = int(project_id)
                    if pid in project_id_map:
                        valid_project_id = int(project_id_map[pid])
                    else:
                        p = q1("SELECT id FROM projects WHERE id=%s AND deleted_at IS NULL", (pid,))
                        if p:
                            valid_project_id = pid
            except Exception:
                valid_project_id = None

            try:
                if folder_id is not None:
                    fid = int(folder_id)
                    f = q1("SELECT id FROM folders WHERE id=%s", (fid,))
                    if f:
                        valid_folder_id = fid
            except Exception:
                valid_folder_id = None

            created_at = _parse_datetime_or_none(item.get("created_at")) or datetime.now()
            completed_at = _parse_datetime_or_none(item.get("completed_at"))
            last_completed_at = _parse_datetime_or_none(item.get("last_completed_at"))
            archived_at = _parse_datetime_or_none(item.get("archived_at")) or datetime.now()
            due_date = _parse_date_or_none(item.get("due_date"))
            due_time = _parse_time_or_none(item.get("due_time"))

            notes = item.get("notes")
            recurrence_rule = item.get("recurrence_rule")

            new_task_id = exec_sql(
                "INSERT INTO tasks("
                "project_id, folder_id, title, notes, due_date, due_time, created_at, completed_at, last_completed_at, recurrence_rule, archived, archived_at"
                ") VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s)",
                (
                    valid_project_id,
                    valid_folder_id,
                    title,
                    notes,
                    due_date,
                    due_time,
                    created_at,
                    completed_at,
                    last_completed_at,
                    recurrence_rule,
                    archived_at,
                ),
            )
            new_task_id = int(new_task_id or 0)

            raw_tags = item.get("tags", [])
            if isinstance(raw_tags, list):
                for tg in raw_tags:
                    tag_name = (str(tg or "")).strip()
                    if not tag_name:
                        continue
                    tag_id = get_or_create_tag(tag_name)
                    exec_sql(
                        "INSERT IGNORE INTO task_tags(task_id, tag_id) VALUES(%s,%s)",
                        (new_task_id, tag_id),
                    )

            imported += 1

        commit()
        flash(
            f"Backup de archivo importado: {imported_projects} proyectos y {imported} tareas cargadas en archivo, {skipped} omitidas.",
            "ok",
        )
    except Exception as e:
        rollback()
        flash(f"No se pudo importar el backup de archivo: {e}", "error")

    return redirect(next_url)


@app.route("/trash", methods=["GET", "POST"])
def trash_view():
    if not admin_required():
        flash("No autorizado.", "error")
        return redirect(url_for("admin"))

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()
        handled = _handle_trash_action(action, "trash_view")
        if handled is not None:
            return handled
        flash("Acción de papelera no válida.", "error")
        return redirect(url_for("trash_view"))

    try:
        task_page = int(request.args.get("task_page", "1"))
    except ValueError:
        task_page = 1
    try:
        project_page = int(request.args.get("project_page", "1"))
    except ValueError:
        project_page = 1

    context = _load_trash_view_data(task_page=task_page, project_page=project_page)
    return render_template("trash.html", **context)


@app.route("/archive", methods=["GET", "POST"])
def archive_view():
    if request.method == "POST":
        action = (request.form.get("action") or "").strip()
        handled = _handle_archive_action(action, "archive_view")
        if handled is not None:
            return handled
        flash("Acción de archivo no válida.", "error")
        return redirect(url_for("archive_view"))

    qtxt = (request.args.get("q") or "").strip()
    archive_legacy_per_page = cfg_int(["app", "pagination", "archive_per_page"], default=25, min_v=5, max_v=500)
    task_per_page = cfg_int(
        ["app", "pagination", "archive_tasks_per_page"],
        default=archive_legacy_per_page,
        min_v=5,
        max_v=500,
    )
    project_per_page = cfg_int(
        ["app", "pagination", "archive_projects_per_page"],
        default=archive_legacy_per_page,
        min_v=5,
        max_v=500,
    )

    try:
        task_page = int(request.args.get("task_page", "1"))
    except ValueError:
        task_page = 1
    task_page = max(task_page, 1)
    task_offset = (task_page - 1) * task_per_page

    try:
        project_page = int(request.args.get("project_page", "1"))
    except ValueError:
        project_page = 1
    project_page = max(project_page, 1)
    project_offset = (project_page - 1) * project_per_page

    sort_by = (request.args.get("sort_by") or "completed").strip().lower()
    if sort_by not in ("archived", "completed", "location"):
        sort_by = "completed"

    sort_dir = (request.args.get("sort_dir") or "desc").strip().lower()
    if sort_dir not in ("asc", "desc"):
        sort_dir = "desc"

    sort_dir_sql = "ASC" if sort_dir == "asc" else "DESC"
    if sort_by == "archived":
        order_sql = f"(t.archived_at IS NULL) ASC, t.archived_at {sort_dir_sql}, t.id DESC"
    elif sort_by == "location":
        order_sql = (
            f"COALESCE(p.name, fd.name, 'Inbox') {sort_dir_sql}, "
            "(t.archived_at IS NULL) ASC, t.archived_at DESC, t.id DESC"
        )
    else:
        order_sql = f"(t.completed_at IS NULL) ASC, t.completed_at {sort_dir_sql}, t.id DESC"

    params_tasks: List[Any] = []
    where_tasks = ["t.archived=1", "t.deleted_at IS NULL"]
    params_projects: List[Any] = []
    where_projects = ["p.archived=1", "p.deleted_at IS NULL"]

    if qtxt:
        like = f"%{qtxt.lower()}%"
        where_tasks.append("LOWER(t.title) LIKE %s")
        where_projects.append("LOWER(p.name) LIKE %s")
        params_tasks.append(like)
        params_projects.append(like)

    where_tasks_sql = " AND ".join(where_tasks)
    where_projects_sql = " AND ".join(where_projects)

    total_tasks_row = q1(
        "SELECT COUNT(*) AS c "
        "FROM tasks t "
        "WHERE " + where_tasks_sql,
        tuple(params_tasks),
    )
    total_tasks = int(total_tasks_row["c"]) if total_tasks_row else 0
    task_pages = max(1, (total_tasks + task_per_page - 1) // task_per_page)

    if task_page > task_pages:
        task_page = task_pages
        task_offset = (task_page - 1) * task_per_page

    archived_tasks = q(
        "SELECT t.id, t.title, t.completed_at, t.archived_at, "
        "p.id AS project_id, p.name AS project_name, "
        "fd.id AS folder_id, fd.name AS folder_name "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=COALESCE(t.folder_id, p.folder_id) "
        "WHERE " + where_tasks_sql + " "
        "ORDER BY " + order_sql + " "
        "LIMIT %s OFFSET %s",
        tuple(params_tasks + [task_per_page, task_offset]),
    )

    total_projects_row = q1(
        "SELECT COUNT(*) AS c "
        "FROM projects p "
        "WHERE " + where_projects_sql,
        tuple(params_projects),
    )
    total_projects = int(total_projects_row["c"]) if total_projects_row else 0
    project_pages = max(1, (total_projects + project_per_page - 1) // project_per_page)

    if project_page > project_pages:
        project_page = project_pages
        project_offset = (project_page - 1) * project_per_page

    archived_projects = q(
        "SELECT p.id, p.name, p.description, p.folder_id, p.archived_at, f.name AS folder_name "
        "FROM projects p "
        "LEFT JOIN folders f ON f.id=p.folder_id "
        "WHERE " + where_projects_sql + " "
        "ORDER BY p.name ASC "
        "LIMIT %s OFFSET %s",
        tuple(params_projects + [project_per_page, project_offset]),
    )

    task_ids = [r["id"] for r in archived_tasks]
    tags_map = load_tags_map(task_ids) if task_ids else {}

    return render_template(
        "archive.html",
        qtxt=qtxt,
        sort_by=sort_by,
        sort_dir=sort_dir,
        archived_tasks=archived_tasks,
        archived_projects=archived_projects,
        tags_map=tags_map,
        task_page=task_page,
        task_pages=task_pages,
        total_tasks=total_tasks,
        project_page=project_page,
        project_pages=project_pages,
        total_projects=total_projects,
        task_per_page=task_per_page,
        project_per_page=project_per_page,
        archive_task_backups=list_archive_task_backups(),
    )
