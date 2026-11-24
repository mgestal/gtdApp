"""Tareas: crear, entrada rápida, editar, borrar, posponer, completar, mover."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any, Optional, Tuple
from zoneinfo import ZoneInfo

from flask import abort, flash, jsonify, redirect, render_template, request, url_for

from reminders import db_time, scheduled_at

from gtd.config import load_config
from gtd.utils import _add_months, _today_madrid, coerce_priority, normalize_name, safe_next_url
from gtd.core import app
from gtd.db import commit, exec_sql, q, q1, rollback
from gtd.lookups import find_folder_by_name, find_project_by_name_active, get_or_create_tag
from gtd.quick_entry import (
    extract_due_date_from_quick,
    extract_due_time_from_quick,
    parse_due_time_token,
    parse_quick_entry,
    parse_tags_csv,
    parse_task_quick_entry,
    RECURRENCE_PATTERNS,
)
from gtd.kanban_board import _project_board_is_initialized, _project_board_status_for_task
from gtd.recurrence import (
    complete_recurring_task_run,
    effective_recurrence_base_due,
    parse_rrule,
    recurrence_due_options,
)
from gtd.calendar_sync import _mark_task_calendar_dirty
from gtd.common import next_project_task_sort_order
from gtd.tasks_ops import complete_nonrecurring_task


@app.route("/tasks/create", methods=["POST"])
def task_create():
    raw = (request.form.get("quick") or "").strip()
    due = request.form.get("due_date") or None
    due_time_raw = (request.form.get("due_time") or "").strip()
    recurrence = (request.form.get("recurrence_rule") or "").strip() or None
    priority = coerce_priority(request.form.get("priority"), default=None)

    if not raw:
        flash("El nombre de la tarea es obligatorio.", "error")
        return redirect(request.referrer or url_for("home"))

    try:
        entry = parse_quick_entry(raw, parse_date=not due, parse_time=not due_time_raw, recurrence=recurrence)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(request.referrer or url_for("home"))
    tags, title, recurrence = entry.tags, entry.title, entry.recurrence
    project_name, folder_name_quick = entry.project, entry.folder
    detected_due_date, detected_due_time, detected_priority = entry.due_date, entry.due_time, entry.priority

    if not title:
        flash("No se detectó un título válido tras extraer etiquetas, fecha, recurrencia y proyecto.", "error")
        return redirect(request.referrer or url_for("home"))

    # 6) Resolver due_date final
    due_date = None
    due_time = None
    if due:
        try:
            due_date = datetime.strptime(due, "%Y-%m-%d").date()
        except ValueError:
            due_date = None
    elif detected_due_date:
        due_date = detected_due_date

    if due_time_raw:
        try:
            due_time = parse_due_time_token(due_time_raw)
        except ValueError:
            flash("Hora inválida. Usa formato HH:MM.", "error")
            return redirect(request.referrer or url_for("home"))
    elif detected_due_time:
        due_time = detected_due_time

    if detected_priority is not None:
        priority = detected_priority

    try:
        project_sel = (request.form.get("project_id") or "").strip()
        folder_sel = (request.form.get("folder_id") or "").strip()

        project_id = None
        folder_id = None

        if project_sel:
            try:
                project_id = int(project_sel)
            except ValueError:
                project_id = None

        elif folder_sel:
            try:
                folder_id = int(folder_sel)
            except ValueError:
                folder_id = None

        else:
            # fallback: #Proyecto o f:Carpeta en texto
            if folder_name_quick:
                folder_id = find_folder_by_name(folder_name_quick)
                # Si no existe la carpeta, no la creamos (a diferencia de proyectos)
            elif project_name:
                project_id = find_project_by_name_active(project_name)

                # Si no existe, lo creamos automáticamente
                if project_id is None:
                    project_id = exec_sql(
                        "INSERT INTO projects(name, archived) VALUES(%s, %s)",
                        (project_name, 0),
                    )

        task_id = exec_sql(
            "INSERT INTO tasks(title, project_id, folder_id, due_date, due_time, recurrence_rule, priority, sort_order) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                title,
                project_id,
                folder_id,
                due_date,
                due_time,
                recurrence,
                priority,
                next_project_task_sort_order(project_id) if project_id else None,
            ),
        )
        requested_board_status = (request.form.get("project_board_status") or "").strip().lower()
        if project_id:
            project_board = q1("SELECT id, board_columns FROM projects WHERE id=%s", (project_id,))
            if requested_board_status or (project_board and _project_board_is_initialized(project_board)):
                _project_board_status_for_task(
                    task_id,
                    project_id,
                    requested_board_status or "brainstorm",
                )

        for t in tags:
            tag_id = get_or_create_tag(t)
            exec_sql(
                "INSERT IGNORE INTO task_tags(task_id, tag_id) VALUES(%s,%s)",
                (task_id, tag_id),
            )

        _mark_task_calendar_dirty(task_id, force_push_if_empty=True)
        commit()

        if project_name and project_id and not project_sel and not folder_sel:
            flash("Tarea creada.", "ok")
        else:
            flash("Tarea creada.", "ok")

    except Exception as e:
        rollback()
        flash(f"No se pudo crear la tarea: {e}", "error")

    return redirect(request.referrer or url_for("home"))


@app.route("/tasks/quick_add", methods=["POST"])
def task_quick_add():
    """
    Alta rápida desde bandeja / carpeta / proyecto.
    - Campo 'quick': título + @etiquetas + opcional #Proyecto
    - project_id o folder_id opcionales (excluyentes).
    - Si en el texto viene #Proyecto:
        * si existe, asigna la tarea a ese proyecto
        * si no existe, crea el proyecto (en la carpeta actual si aplica)
          y asigna la tarea a ese proyecto
    """
    raw = (request.form.get("quick") or "").strip()
    due_time_raw = (request.form.get("due_time") or "").strip()
    priority = coerce_priority(request.form.get("priority"), default=None)
    if not raw:
        flash("El nombre de la tarea es obligatorio.", "error")
        return redirect(request.form.get("next") or request.referrer or url_for("home"))

    try:
        entry = parse_quick_entry(raw, parse_time=not due_time_raw)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(request.form.get("next") or request.referrer or url_for("home"))

    due_time = None
    if due_time_raw:
        try:
            due_time = parse_due_time_token(due_time_raw)
        except ValueError:
            flash("Hora inválida. Usa formato HH:MM.", "error")
            return redirect(request.form.get("next") or request.referrer or url_for("home"))
    else:
        due_time = entry.due_time

    if entry.priority is not None:
        priority = entry.priority

    title, tags, due_date, recurrence = entry.title, entry.tags, entry.due_date, entry.recurrence
    quick_project_name, quick_folder_name = entry.project, entry.folder
    
    if not title:
        flash("No se detectó un título válido (deja texto fuera de @etiquetas, #proyecto y f:carpeta).", "error")
        return redirect(request.form.get("next") or request.referrer or url_for("home"))

    project_id_raw = (request.form.get("project_id") or "").strip()
    folder_id_raw = (request.form.get("folder_id") or "").strip()

    project_id = None
    folder_id = None

    if project_id_raw:
        try:
            project_id = int(project_id_raw)
        except ValueError:
            project_id = None
        folder_id = None

    elif folder_id_raw:
        try:
            folder_id = int(folder_id_raw)
        except ValueError:
            folder_id = None
        project_id = None

    try:
        # 1) Resolver asignación textual cuando no hay proyecto explícito.
        #    Si venimos de carpeta (folder_id), #proyecto debe poder crear
        #    el proyecto dentro de esa carpeta y asignar la tarea ahí.
        #    Prioridad: f:carpeta > #proyecto
        if project_id is None:
            if quick_folder_name:
                folder_id = find_folder_by_name(quick_folder_name)
            elif quick_project_name:
                existing_project_id = find_project_by_name_active(quick_project_name)

                if existing_project_id is not None:
                    project_id = existing_project_id
                    folder_id = None
                else:
                    project_id = exec_sql(
                        "INSERT INTO projects(name, folder_id, archived) VALUES(%s,%s,%s)",
                        (quick_project_name, folder_id, 0),
                    )
                    folder_id = None

        # 2) Insertar tarea:
        #    - si hay project_id => la tarea va al proyecto
        #    - si no hay project_id => va a la carpeta (si existe)
        task_id = exec_sql(
            "INSERT INTO tasks(title, project_id, folder_id, due_date, due_time, recurrence_rule, priority, sort_order) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                title,
                project_id,
                folder_id,
                due_date,
                due_time,
                recurrence,
                priority,
                next_project_task_sort_order(project_id) if project_id else None,
            ),
        )

        # 3) Etiquetas
        for t in tags:
            tag_id = get_or_create_tag(t)
            exec_sql(
                "INSERT IGNORE INTO task_tags(task_id, tag_id) VALUES(%s,%s)",
                (task_id, tag_id),
            )

        _mark_task_calendar_dirty(task_id, force_push_if_empty=True)
        commit()
        flash("Tarea creada.", "ok")

    except Exception as e:
        rollback()
        flash(f"No se pudo crear la tarea: {e}", "error")

    return redirect(request.form.get("next") or request.referrer or url_for("home"))
    


@app.route("/tasks/<int:task_id>/edit", methods=["GET", "POST"])
def task_edit(task_id: int):
    # ---------- helpers ----------
    # Reutilizamos safe_next_url global para consistencia entre rutas.

    # ---------- load task ----------
    task = q1(
        "SELECT id, title, notes, location, due_date, due_time, project_id, folder_id, recurrence_rule, max_occurrences, priority, completed_at, archived, deleted_at "
        "FROM tasks WHERE id=%s",
        (task_id,),
    )
    if not task:
        abort(404)

    # ✅ next_url SIEMPRE disponible (GET/POST) + normalizado con script_root
    next_url = request.args.get("next") or request.form.get("next") or url_for("home")
    next_url = safe_next_url(next_url)

    # ---------- reference data ----------
    projects = q("SELECT id, name FROM projects WHERE archived=0 ORDER BY name")
    folders = q("SELECT id, parent_id, name FROM folders ORDER BY name")

    current_tags = q(
        "SELECT tg.id, tg.name "
        "FROM task_tags ttag JOIN tags tg ON tg.id=ttag.tag_id "
        "WHERE ttag.task_id=%s ORDER BY tg.name",
        (task_id,),
    )
    tags_csv = ", ".join(["@" + t["name"] for t in current_tags])

    # ✅ Subtareas siempre disponibles (GET y POST render)
    subs = q(
        "SELECT id, task_id, title, description, due_date, completed_at, created_at "
        "FROM subtasks WHERE task_id=%s "
        "ORDER BY (completed_at IS NOT NULL) ASC, (due_date IS NULL) ASC, due_date ASC, id ASC",
        (task_id,),
    )
    cnt = q1(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN completed_at IS NOT NULL THEN 1 ELSE 0 END) AS done "
        "FROM subtasks WHERE task_id=%s",
        (task_id,),
    ) or {"total": 0, "done": 0}

    # ---------- POST: update ----------
    if request.method == "POST":
        raw_title = normalize_name(request.form.get("title", ""))
        notes = (request.form.get("notes") or "").strip() or None
        location = (request.form.get("location") or "").strip() or None
        due_raw = (request.form.get("due_date") or "").strip()
        due_time_raw = (request.form.get("due_time") or "").strip()
        recurrence = (request.form.get("recurrence_rule") or "").strip() or None
        max_occurrences_raw = (request.form.get("max_occurrences") or "").strip()
        max_occurrences = None
        if max_occurrences_raw:
            try:
                max_occurrences = int(max_occurrences_raw)
                if max_occurrences < 1:
                    max_occurrences = None
            except ValueError:
                flash("Número máximo de repeticiones inválido.", "error")
                return redirect(url_for("task_edit", task_id=task_id, next=next_url))
        priority = coerce_priority(request.form.get("priority"), default=task.get("priority"))

        project_raw = (request.form.get("project_id") or "").strip()
        folder_raw = (request.form.get("folder_id") or "").strip()
        tags_csv_form = request.form.get("tags_csv") or ""

        if not raw_title:
            flash("El título es obligatorio.", "error")
            return redirect(url_for("task_edit", task_id=task_id, next=next_url))

        # ── Parsear el título en busca de @etiquetas, #proyecto, fecha y recurrencia ──

        raw_work = raw_title

        # 2) Extraer fecha del título (solo si el campo due_date está vacío)
        detected_due_date = None
        detected_due_time = None
        if not due_raw:
            try:
                detected_due_date, raw_work = extract_due_date_from_quick(raw_work)
            except ValueError as e:
                flash(str(e), "error")
                return redirect(url_for("task_edit", task_id=task_id, next=next_url))
        if not due_time_raw:
            try:
                detected_due_time, raw_work = extract_due_time_from_quick(raw_work)
            except ValueError as e:
                flash(str(e), "error")
                return redirect(url_for("task_edit", task_id=task_id, next=next_url))

        # 3) Extraer recurrencia del título (solo si el campo recurrence_rule está vacío)
        if not recurrence:
            for pattern, rule in RECURRENCE_PATTERNS.items():
                if re.search(pattern, raw_work, flags=re.IGNORECASE):
                    recurrence = rule
                    raw_work = re.sub(pattern, '', raw_work, flags=re.IGNORECASE)
                    break

        # 4) Extraer @etiquetas, #proyecto y f:carpeta con el mismo parser que alta rápida.
        clean_title, parsed_tags, quick_project_name, quick_folder_name = parse_task_quick_entry(raw_work)
        clean_title = clean_title.strip(" -_,.;:")

        if not clean_title:
            flash("No se detectó un título válido tras extraer etiquetas, fecha y proyecto.", "error")
            return redirect(url_for("task_edit", task_id=task_id, next=next_url))

        # ── Resolver due_date final ──
        due_date = None
        due_time = None
        if due_raw:
            try:
                due_date = datetime.strptime(due_raw, "%Y-%m-%d").date()
            except ValueError:
                flash("Fecha inválida.", "error")
                return redirect(url_for("task_edit", task_id=task_id, next=next_url))
        elif detected_due_date:
            due_date = detected_due_date

        if due_time_raw:
            try:
                due_time = parse_due_time_token(due_time_raw)
            except ValueError:
                flash("Hora inválida. Usa formato HH:MM.", "error")
                return redirect(url_for("task_edit", task_id=task_id, next=next_url))
        elif detected_due_time:
            due_time = detected_due_time

        # ── Resolver proyecto / carpeta ──
        submitted_project_id = None
        submitted_folder_id = None

        if project_raw:
            try:
                submitted_project_id = int(project_raw)
            except ValueError:
                submitted_project_id = None
        elif folder_raw:
            try:
                submitted_folder_id = int(folder_raw)
            except ValueError:
                submitted_folder_id = None

        original_project_id = int(task.get("project_id")) if task.get("project_id") is not None else None
        original_folder_id = int(task.get("folder_id")) if task.get("folder_id") is not None else None

        form_selection_changed = (
            submitted_project_id != original_project_id
            or submitted_folder_id != original_folder_id
        )

        project_id = submitted_project_id
        folder_id = submitted_folder_id

        # Si el usuario no tocó los selects, el token rápido del título puede mover
        # la tarea de carpeta/proyecto igual que en el alta rápida.
        if not form_selection_changed:
            if quick_folder_name:
                folder_id = find_folder_by_name(quick_folder_name)
                project_id = None
            elif quick_project_name:
                project_id = find_project_by_name_active(quick_project_name)
                if project_id is None:
                    project_id = exec_sql(
                        "INSERT INTO projects(name, archived) VALUES(%s, %s)",
                        (quick_project_name, 0),
                    )
                folder_id = None

        if project_id:
            folder_id = None
        elif folder_id:
            project_id = None

        # ── Combinar etiquetas: las del formulario tags_csv + las extraídas del título ──
        tags_from_form = parse_tags_csv(tags_csv_form)
        existing_lower = {t.lower() for t in tags_from_form}
        for t in parsed_tags:
            t_norm = normalize_name(t)
            if t_norm and t_norm.lower() not in existing_lower:
                tags_from_form.append(t_norm)
                existing_lower.add(t_norm.lower())

        try:
            exec_sql(
                "UPDATE tasks "
                "SET title=%s, notes=%s, location=%s, due_date=%s, due_time=%s, project_id=%s, folder_id=%s, recurrence_rule=%s, max_occurrences=%s, priority=%s "
                "WHERE id=%s",
                (clean_title, notes, location, due_date, due_time, project_id, folder_id, recurrence, max_occurrences, priority, task_id),
            )

            # Tags: borrar y reinsertar
            exec_sql("DELETE FROM task_tags WHERE task_id=%s", (task_id,))
            for tname in tags_from_form:
                tid = get_or_create_tag(tname)
                exec_sql(
                    "INSERT IGNORE INTO task_tags(task_id, tag_id) VALUES(%s,%s)",
                    (task_id, tid),
                )

            _mark_task_calendar_dirty(task_id)
            commit()
            flash("Tarea actualizada.", "ok")
            return redirect(next_url)

        except Exception as e:
            rollback()
            flash(f"No se pudo actualizar: {e}", "error")
            return redirect(url_for("task_edit", task_id=task_id, next=next_url))

    reminder_rows = q(
        "SELECT * FROM task_reminders WHERE task_id=%s ORDER BY id", (task_id,)
    )
    tz = ZoneInfo(load_config().get("app", {}).get("timezone", "Europe/Madrid"))
    reminder_project = q1("SELECT archived, deleted_at FROM projects WHERE id=%s", (task["project_id"],)) if task["project_id"] else None
    reminders_paused = bool(task["completed_at"] or task["archived"] or task["deleted_at"]
                            or (task["project_id"] and (not reminder_project or reminder_project["archived"]
                                                        or reminder_project["deleted_at"])))
    for reminder in reminder_rows:
        reminder.update(due_date=task.get("due_date"), due_time=task.get("due_time"))
        try:
            when = scheduled_at(reminder, tz)
            reminder["local_when"] = (
                when.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).strftime("%d-%m-%Y %H:%M")
                if when else None
            )
            reminder["local_input"] = (
                when.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).strftime("%Y-%m-%dT%H:%M")
                if when else ""
            )
            reminder["delivery"] = q1(
                "SELECT status, last_error FROM reminder_deliveries "
                "WHERE reminder_id=%s AND scheduled_at_utc=%s",
                (reminder["id"], when),
            ) if when else None
            reminder["push_delivery"] = q1(
                "SELECT o.status, COUNT(d.id) AS total, "
                "SUM(d.status='sent') AS sent, SUM(d.status IN ('failed','invalid')) AS failed, "
                "SUM(d.status='pending') AS pending FROM push_occurrences o "
                "LEFT JOIN push_deliveries d ON d.occurrence_id=o.id "
                "WHERE o.reminder_id=%s AND o.scheduled_at_utc=%s GROUP BY o.id, o.status",
                (reminder["id"], when),
            ) if when else None
        except ValueError as exc:
            reminder["schedule_error"] = str(exc)
        reminder["date_only_time"] = db_time(reminder["date_only_time"])

    # ---------- GET render ----------
    return render_template(
        "task_edit.html",
        task=task,
        projects=projects,
        folders=folders,
        tags_csv=tags_csv,
        subtasks=subs,
        sub_counts=cnt,
        next_url=next_url,
        reminders=reminder_rows,
        reminders_paused=reminders_paused,
    )


@app.route("/tasks/<int:task_id>/delete", methods=["POST"])
def task_delete(task_id: int):
    task = q1("SELECT id, archived FROM tasks WHERE id=%s", (task_id,))
    if not task:
        abort(404)

    try:
        prev_archived = int(task.get("archived") or 0)
        exec_sql(
            "UPDATE tasks "
            "SET deleted_prev_archived=%s, deleted_at=NOW(), archived=1, archived_at=COALESCE(archived_at, NOW()) "
            "WHERE id=%s",
            (prev_archived, task_id),
        )
        _mark_task_calendar_dirty(task_id)

        commit()
        flash("Tarea enviada a la papelera.", "ok")
    except Exception as e:
        rollback()
        # Esto hace que el error real aparezca en error.log de Apache/mod_wsgi
        try:
            app.logger.exception("task_delete failed for task_id=%s", task_id)
        except Exception:
            pass
        flash(f"No se pudo enviar la tarea a papelera: {e}", "error")

    next_url = safe_next_url(request.form.get("next"), "home")
    return redirect(next_url)


@app.route("/tasks/<int:task_id>/postpone", methods=["POST"])
def task_postpone(task_id: int):
    task = q1("SELECT id, due_date FROM tasks WHERE id=%s", (task_id,))
    if not task:
        abort(404)

    today = _today_madrid()
    base = task.get("due_date")
    if isinstance(base, datetime):
        base = base.date()
    if not isinstance(base, date):
        base = today

    mode = (request.form.get("mode") or "").strip().lower()
    new_due: Optional[date] = None

    if mode == "tomorrow":
        new_due = today + timedelta(days=1)
    elif mode == "monday":
        new_due = today + timedelta(days=7 - today.weekday())
    elif mode == "week":
        new_due = base + timedelta(days=7)
        while new_due <= today:
            new_due += timedelta(days=7)
    elif mode == "month":
        n = 1
        new_due = _add_months(base, n)
        while new_due <= today:
            n += 1
            new_due = _add_months(base, n)
    elif mode == "manual":
        try:
            new_due = datetime.strptime((request.form.get("date") or "").strip(), "%Y-%m-%d").date()
        except ValueError:
            new_due = None

    if new_due is None:
        flash("Fecha de aplazamiento no válida.", "error")
        return redirect(safe_next_url(request.form.get("next"), "home"))

    try:
        exec_sql("UPDATE tasks SET due_date=%s WHERE id=%s", (new_due, task_id))
        _mark_task_calendar_dirty(task_id)
        commit()
        flash(f"Tarea pospuesta al {new_due.strftime('%d-%m-%Y')}.", "ok")
    except Exception as e:
        rollback()
        app.logger.exception("task_postpone failed for task_id=%s", task_id)
        flash(f"No se pudo posponer la tarea: {e}", "error")

    return redirect(safe_next_url(request.form.get("next"), "home"))


@app.route("/tasks/<int:task_id>/toggle", methods=["POST"])
def task_toggle(task_id: int):
    task = q1(
        "SELECT t.id, t.completed_at, t.due_date, t.recurrence_rule, t.max_occurrences, t.project_id, t.archived, "
        "rr.last_next_due_date "
        "FROM tasks t "
        "LEFT JOIN ("
        "SELECT task_id, MAX(next_due_date) AS last_next_due_date "
        "FROM recurring_task_runs "
        "GROUP BY task_id"
        ") rr ON rr.task_id=t.id "
        "WHERE t.id=%s",
        (task_id,),
    )
    if not task:
        abort(404)

    now = datetime.now(ZoneInfo("Europe/Madrid")).replace(tzinfo=None)  # guardamos naive en DB

    try:
        if task["completed_at"]:
            # Desmarcar: funciona exactamente como ahora
            exec_sql("UPDATE tasks SET completed_at=NULL WHERE id=%s", (task_id,))
            _project_board_status_for_task(task_id, task.get("project_id"), "brainstorm")

        else:
            # CASO 1: tarea recurrente -> comportamiento actual, sin tocar NextAction
            if task.get("recurrence_rule") and task.get("due_date"):
                recurrence_due_choice = (request.form.get("recurrence_due_choice") or "").strip().lower()
                complete_recurring_task_run(task_id, task, now, recurrence_due_choice)

            else:
                # CASO 2: tarea no recurrente
                complete_nonrecurring_task(task_id, task, now)

        _mark_task_calendar_dirty(task_id)
        commit()

    except Exception as e:
        rollback()
        flash(f"No se pudo actualizar la tarea: {e}", "error")

    return redirect(request.referrer or url_for("home"))


@app.route("/tasks/<int:task_id>/unarchive", methods=["POST"])
def task_unarchive(task_id: int):
    task = q1("SELECT id FROM tasks WHERE id=%s", (task_id,))
    if not task:
        abort(404)

    try:
        exec_sql(
            "UPDATE tasks "
            "SET archived=0, archived_at=NULL, deleted_at=NULL, deleted_prev_archived=0 "
            "WHERE id=%s",
            (task_id,),
        )
        _mark_task_calendar_dirty(task_id)
        commit()
        flash("Tarea desarchivada.", "ok")
    except Exception as e:
        rollback()
        flash(f"No se pudo desarchivar la tarea: {e}", "error")

    next_url = safe_next_url(request.form.get("next"), "archive_view")
    return redirect(next_url)


@app.route("/api/tasks/<int:task_id>/move", methods=["POST"])
def api_task_move(task_id: int):
    payload = request.get_json(silent=True) or {}
    target_type = (payload.get("target_type") or "").strip().lower()
    target_id_raw = payload.get("target_id")

    task = q1("SELECT id FROM tasks WHERE id=%s AND deleted_at IS NULL", (task_id,))
    if not task:
        return jsonify({"ok": False, "error": "Tarea no encontrada"}), 404

    try:
        target_id = int(target_id_raw)
    except Exception:
        return jsonify({"ok": False, "error": "Destino inválido"}), 400

    def update_task_location(sql: str, params: Tuple[Any, ...]) -> None:
        for attempt in range(2):
            try:
                exec_sql(sql, params)
            except Exception as exc:
                rollback()
                error_code = exc.args[0] if getattr(exc, "args", ()) else None
                if error_code != 1020 or attempt == 1:
                    raise
                continue
            commit()
            return

    try:
        if target_type == "project":
            project = q1(
                "SELECT id FROM projects WHERE id=%s AND archived=0 AND deleted_at IS NULL",
                (target_id,),
            )
            if not project:
                return jsonify({"ok": False, "error": "Proyecto destino no válido"}), 400
            update_task_location(
                "UPDATE tasks SET project_id=%s, folder_id=NULL, sort_order=%s WHERE id=%s",
                (target_id, next_project_task_sort_order(target_id), task_id),
            )
            return jsonify({"ok": True, "message": "Tarea movida a proyecto"})

        if target_type == "folder":
            folder = q1("SELECT id FROM folders WHERE id=%s", (target_id,))
            if not folder:
                return jsonify({"ok": False, "error": "Carpeta destino no válida"}), 400
            update_task_location(
                "UPDATE tasks SET folder_id=%s, project_id=NULL WHERE id=%s",
                (target_id, task_id),
            )
            return jsonify({"ok": True, "message": "Tarea movida a carpeta"})

        return jsonify({"ok": False, "error": "Tipo de destino no soportado"}), 400
    except Exception as e:
        rollback()
        if getattr(e, "args", ()) and e.args[0] == 1020:
            return jsonify({"ok": False, "error": "La tarea cambió mientras se movía. Vuelve a intentarlo."}), 409
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/tasks/<int:task_id>/toggle_preview")
def api_task_toggle_preview(task_id: int):
    task = q1(
        "SELECT t.id, t.title, t.completed_at, t.due_date, t.recurrence_rule, rr.last_next_due_date "
        "FROM tasks t "
        "LEFT JOIN ("
        "SELECT task_id, MAX(next_due_date) AS last_next_due_date "
        "FROM recurring_task_runs "
        "GROUP BY task_id"
        ") rr ON rr.task_id=t.id "
        "WHERE t.id=%s",
        (task_id,),
    )
    if not task:
        abort(404)

    # Si está completada o no es periódica con fecha, no hay nada que decidir.
    if task.get("completed_at") or not task.get("recurrence_rule") or not task.get("due_date"):
        return jsonify({"requires_choice": False})

    rule = parse_rrule(task["recurrence_rule"])
    if not rule:
        return jsonify({"requires_choice": False})

    today_d = datetime.now(ZoneInfo("Europe/Madrid")).date()

    # Base efectiva: due_date o último next_due_date registrado, la más reciente.
    base_date = effective_recurrence_base_due(task)
    if not base_date:
        return jsonify({"requires_choice": False})

    due_options = recurrence_due_options(base_date, rule, today_d)
    if not due_options["requires_choice"]:
        return jsonify({"requires_choice": False})

    return jsonify(
        {
            "requires_choice": True,
            "task_id": int(task["id"]),
            "title": task.get("title") or "",
            "today": today_d.isoformat(),
            "option_a_due": due_options["option_a_due"].isoformat(),
            "option_b_due": due_options["option_b_due"].isoformat(),
        }
    )
