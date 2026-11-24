"""Proyectos: listado, detalle, tablero, edición, mover/archivar/borrar, exportar/importar."""
from __future__ import annotations

import csv
import io
import json
import re
from xml.etree import ElementTree as ET

from flask import abort, flash, jsonify, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

from subtasks import load_subtask_counts, load_subtasks_map

from gtd.config import cfg_bool, cfg_int
from gtd.utils import _parse_date_iso, _parse_dt_iso, _split_tags_csv, normalize_name, safe_next_url
from gtd.core import app
from gtd.db import commit, exec_sql, q, q1, rollback
from gtd.lookups import get_or_create_tag
from gtd.roles import non_active_project_folder_ids
from gtd.kanban_board import (
    _initialize_project_board,
    _project_board_column_key,
    _project_board_columns,
    _project_board_is_initialized,
    _remove_task_tag,
    _save_project_board_columns,
    DEFAULT_PROJECT_BOARD_COLUMNS,
)
from gtd.recurrence import recurring_effective_due_sql
from gtd.common import build_folder_breadcrumb, load_tags_map, subdb


@app.route("/projects")
def projects():
    qtxt = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "active").strip().lower()
    scope = (request.args.get("scope") or "").strip().lower()

    if status not in ("active", "archived"):
        status = "active"

    per_page = cfg_int(["app", "pagination", "projects_per_page"], default=15, min_v=5, max_v=500)

    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    page = max(1, page)
    offset = (page - 1) * per_page

    where_parts = []
    params = []

    if qtxt:
        where_parts.append("(LOWER(p.name) LIKE %s OR LOWER(COALESCE(p.description, '')) LIKE %s OR LOWER(COALESCE(f.name, '')) LIKE %s)")
        like = f"%{qtxt.lower()}%"
        params.extend([like, like, like])

    if status == "archived":
        where_parts.append("p.archived=1")
    else:
        where_parts.append("p.archived=0")

    # Scope opcional usado por el KPI del dashboard: excluye ciertos árboles de carpetas.
    if status == "active" and scope == "dashboard_active":
        # Rutinas/Agenda/Sometime/ADTV/Seguimiento (roles GTD) y sus subcarpetas.
        excluded_folder_ids: set[int] = non_active_project_folder_ids()

        if excluded_folder_ids:
            placeholders = ",".join(["%s"] * len(excluded_folder_ids))
            where_parts.append(f"(p.folder_id IS NULL OR p.folder_id NOT IN ({placeholders}))")
            params.extend(sorted(excluded_folder_ids))

    where_sql = ""
    if where_parts:
        where_sql = "WHERE " + " AND ".join(where_parts)

    total_row = q1(
        "SELECT COUNT(*) AS c "
        "FROM projects p "
        "LEFT JOIN folders f ON f.id=p.folder_id "
        f"{where_sql}",
        tuple(params),
    )
    total = int(total_row["c"]) if total_row else 0
    pages = max(1, (total + per_page - 1) // per_page)

    if page > pages:
        page = pages
        offset = (page - 1) * per_page

    rows = q(
        "SELECT p.id, p.name, p.description, p.archived, p.folder_id, f.name AS folder_name "
        "FROM projects p "
        "LEFT JOIN folders f ON f.id=p.folder_id "
        f"{where_sql} "
        "ORDER BY p.archived ASC, p.name ASC "
        "LIMIT %s OFFSET %s",
        tuple(params + [per_page, offset]),
    )

    folders = q("SELECT id, parent_id, name FROM folders ORDER BY name")

    return render_template(
        "projects.html",
        rows=rows,
        folders=folders,
        qtxt=qtxt,
        status=status,
        scope=scope,
        page=page,
        pages=pages,
        total=total,
        per_page=per_page,
    )

@app.route("/projects/<int:project_id>")
def project_detail(project_id: int):
    project = q1("SELECT id, name, description, archived, archived_at, folder_id, auto_promote_nextaction, board_visible, board_columns FROM projects WHERE id=%s", (project_id,))
    if not project:
        abort(404)
    recurring_due_join, effective_due_expr = recurring_effective_due_sql("t")

    folder_breadcrumb = build_folder_breadcrumb(project.get("folder_id"), include_self=True)

    active_tasks = q(
        f"SELECT t.id, t.title, t.location, t.notes, {effective_due_expr} AS due_date, t.due_time, t.completed_at, t.recurrence_rule, t.priority, t.sort_order, t.project_board_status, t.project_board_order "
        "FROM tasks t "
        f"{recurring_due_join}"
        "WHERE project_id=%s AND completed_at IS NULL AND archived=0 AND deleted_at IS NULL "
        "ORDER BY COALESCE(sort_order, 2147483647) ASC, id ASC",
        (project_id,),
    )

    done_tasks = q(
        "SELECT id, title, notes, due_date, due_time, completed_at, recurrence_rule, priority, project_board_status, project_board_order "
        "FROM tasks "
        "WHERE project_id=%s AND completed_at IS NOT NULL AND archived=0 AND deleted_at IS NULL "
        "ORDER BY completed_at DESC, id",
        (project_id,),
    )

    # tags y subtareas para ambas listas
    all_ids = [t["id"] for t in active_tasks] + [t["id"] for t in done_tasks]
    tags_map = load_tags_map(all_ids) if all_ids else {}
    sub_counts = load_subtask_counts(subdb, all_ids)
    sub_map = load_subtasks_map(subdb, all_ids)
    if project.get("board_visible") and not _project_board_is_initialized(project):
        _initialize_project_board(project_id)
        commit()
        project["board_columns"] = json.dumps(DEFAULT_PROJECT_BOARD_COLUMNS)
    project_kanban_columns = _project_board_columns(project) if project.get("board_visible") else []
    project_tasks_by_status = {column["key"]: [] for column in project_kanban_columns}
    if project.get("board_visible"):
        for task in active_tasks + done_tasks:
            status = task.get("project_board_status") or ("done" if task.get("completed_at") else "brainstorm")
            if status not in project_tasks_by_status:
                status = "done" if task.get("completed_at") else "brainstorm"
            project_tasks_by_status[status].append(task)

    # Valor efectivo del auto-promote: override de proyecto si está definido, si no el global
    proj_override = project.get("auto_promote_nextaction")
    if proj_override is not None:
        promote_effective = bool(proj_override)
    else:
        promote_effective = cfg_bool(
            ["app", "behavior", "promote_nextaction_on_complete"],
            default=True,
        )

    return render_template(
        "project_detail.html",
        project=project,
        folder_breadcrumb=folder_breadcrumb,
        active_tasks=active_tasks,
        done_tasks=done_tasks,
        tags_map=tags_map,
        sub_counts=sub_counts, sub_map=sub_map,
        promote_effective=promote_effective,
        project_kanban_columns=project_kanban_columns,
        project_tasks_by_status=project_tasks_by_status,
    )


@app.route("/projects/<int:project_id>/board-toggle", methods=["POST"])
def project_board_toggle(project_id: int):
    project = q1("SELECT id, board_visible, board_columns FROM projects WHERE id=%s", (project_id,))
    if not project:
        abort(404)

    try:
        if not project.get("board_visible"):
            _initialize_project_board(project_id)
        exec_sql(
            "UPDATE projects SET board_visible=1-board_visible, updated_at=NOW() WHERE id=%s",
            (project_id,),
        )
        commit()
        flash("Visibilidad del proyecto en el tablero actualizada.", "ok")
    except Exception as e:
        rollback()
        flash(f"No se pudo actualizar el tablero del proyecto: {e}", "error")

    return redirect(url_for("project_detail", project_id=project_id))


@app.route("/api/projects/<int:project_id>/board/columns", methods=["POST"])
def api_project_board_column_create(project_id: int):
    project = q1("SELECT id, board_columns FROM projects WHERE id=%s", (project_id,))
    if not project:
        return jsonify({"ok": False, "error": "Proyecto no encontrado"}), 404
    label = normalize_name((request.get_json(silent=True) or {}).get("label"))
    if not label:
        return jsonify({"ok": False, "error": "Indica un nombre para la columna"}), 400
    try:
        columns = _project_board_columns(project)
        columns.insert(max(0, len(columns) - 1), {"key": _project_board_column_key(label, columns), "label": label})
        _save_project_board_columns(project_id, columns)
        commit()
        return jsonify({"ok": True, "columns": columns})
    except Exception as exc:
        rollback()
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/projects/<int:project_id>/board/columns/<key>", methods=["POST", "DELETE"])
def api_project_board_column_update(project_id: int, key: str):
    project = q1("SELECT id, board_columns FROM projects WHERE id=%s", (project_id,))
    if not project:
        return jsonify({"ok": False, "error": "Proyecto no encontrado"}), 404
    try:
        columns = _project_board_columns(project)
        column = next((item for item in columns if item["key"] == key), None)
        if not column:
            return jsonify({"ok": False, "error": "Columna no encontrada"}), 404
        if request.method == "POST":
            label = normalize_name((request.get_json(silent=True) or {}).get("label"))
            if not label:
                return jsonify({"ok": False, "error": "Indica un nombre para la columna"}), 400
            column["label"] = label
        elif key == "done":
            columns = [item for item in columns if item["key"] != "done"]
        else:
            columns = [item for item in columns if item["key"] != key]
            exec_sql(
                "UPDATE tasks SET project_board_status='brainstorm' WHERE project_id=%s AND project_board_status=%s",
                (project_id, key),
            )
        _save_project_board_columns(project_id, columns)
        commit()
        return jsonify({"ok": True, "columns": _project_board_columns({"board_columns": columns})})
    except Exception as exc:
        rollback()
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/projects/<int:project_id>/board/columns/reorder", methods=["POST"])
def api_project_board_columns_reorder(project_id: int):
    project = q1("SELECT id, board_columns FROM projects WHERE id=%s", (project_id,))
    if not project:
        return jsonify({"ok": False, "error": "Proyecto no encontrado"}), 404
    payload = request.get_json(silent=True) or {}
    ordered_keys = payload.get("keys") or []
    columns = _project_board_columns(project)
    if set(ordered_keys) != {column["key"] for column in columns}:
        return jsonify({"ok": False, "error": "Orden de columnas inválido"}), 400
    ordered_columns = [next(column for column in columns if column["key"] == key) for key in ordered_keys]
    try:
        _save_project_board_columns(project_id, ordered_columns)
        commit()
        return jsonify({"ok": True})
    except Exception as exc:
        rollback()
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/projects/<int:project_id>/board/tasks/<int:task_id>/status", methods=["POST"])
def api_project_board_task_status(project_id: int, task_id: int):
    task = q1(
        "SELECT id, project_id FROM tasks WHERE id=%s AND project_id=%s AND deleted_at IS NULL",
        (task_id, project_id),
    )
    if not task:
        return jsonify({"ok": False, "error": "Tarea no encontrada"}), 404
    project = q1("SELECT id, board_columns FROM projects WHERE id=%s", (project_id,))
    status = str((request.get_json(silent=True) or {}).get("status") or "").strip().lower()
    valid_statuses = {column["key"] for column in _project_board_columns(project or {})}
    if status not in valid_statuses:
        return jsonify({"ok": False, "error": "Estado inválido"}), 400
    try:
        previous = q1("SELECT project_board_status FROM tasks WHERE id=%s", (task_id,))
        was_in_progress = (previous or {}).get("project_board_status") == "in-progress"
        completed_at = "NOW()" if status == "done" else "NULL"
        exec_sql(
            f"UPDATE tasks SET completed_at={completed_at}, project_board_status=%s, project_board_order=id WHERE id=%s",
            (status, task_id),
        )
        if status == "in-progress":
            exec_sql(
                "INSERT IGNORE INTO task_tags(task_id, tag_id) VALUES(%s,%s)",
                (task_id, get_or_create_tag("EnCurso")),
            )
        elif was_in_progress:
            _remove_task_tag(task_id, "EnCurso")
        commit()
        return jsonify({"ok": True, "status": status})
    except Exception as exc:
        rollback()
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/projects/<int:project_id>/edit", methods=["GET", "POST"])
def project_edit(project_id: int):
    proj = q1("SELECT id, name, description, folder_id, archived, auto_promote_nextaction FROM projects WHERE id=%s", (project_id,))
    if not proj:
        abort(404)

    folders = q("SELECT id, parent_id, name FROM folders ORDER BY name")

    if request.method == "POST":
        name = normalize_name(request.form.get("name", ""))
        desc = (request.form.get("description") or "").strip() or None
        folder_raw = request.form.get("folder_id") or ""
        folder_id = int(folder_raw) if folder_raw else None
        # El checkbox envía "1" si está marcado, nada si no → guardar explícitamente 1 o 0
        auto_promote_raw = request.form.get("auto_promote_nextaction")
        auto_promote = 1 if auto_promote_raw == "1" else 0

        if not name:
            flash("El nombre del proyecto es obligatorio.", "error")
            return redirect(url_for("project_edit", project_id=project_id))

        try:
            exec_sql(
                "UPDATE projects SET name=%s, description=%s, folder_id=%s, auto_promote_nextaction=%s, updated_at=NOW() WHERE id=%s",
                (name, desc, folder_id, auto_promote, project_id),
            )
            commit()
            flash("Proyecto actualizado.", "ok")
            return redirect(url_for("project_detail", project_id=project_id))

        except Exception as e:
            rollback()
            flash(f"No se pudo actualizar el proyecto: {e}", "error")
            return redirect(url_for("project_edit", project_id=project_id))

    # Calcular el valor por defecto global para mostrar en el formulario
    global_promote_default = cfg_bool(
        ["app", "behavior", "promote_nextaction_on_complete"],
        default=True,
    )
    return render_template("project_edit.html", project=proj, folders=folders, global_promote_default=global_promote_default)


@app.route("/projects/<int:project_id>/move", methods=["POST"])
def project_move(project_id: int):
    """Atajo para mover desde listados (solo carpeta)."""
    folder_raw = request.form.get("folder_id") or ""
    folder_id = int(folder_raw) if folder_raw else None

    proj = q1("SELECT id FROM projects WHERE id=%s", (project_id,))
    if not proj:
        abort(404)

    try:
        exec_sql("UPDATE projects SET folder_id=%s, updated_at=NOW() WHERE id=%s", (folder_id, project_id))
        commit()
        flash("Proyecto movido.", "ok")
    except Exception as e:
        rollback()
        flash(f"No se pudo mover el proyecto: {e}", "error")

    return redirect(request.referrer or url_for("projects"))


@app.route("/projects/<int:project_id>/unarchive", methods=["POST"])
def project_unarchive(project_id: int):
    try:
        exec_sql(
            "UPDATE projects "
            "SET archived=0, archived_at=NULL, deleted_at=NULL, deleted_prev_archived=0, updated_at=NOW() "
            "WHERE id=%s",
            (project_id,),
        )
        commit()
        flash("Proyecto desarchivado (activo de nuevo).", "ok")
    except Exception as e:
        rollback()
        flash(f"No se pudo desarchivar: {e}", "error")
    return redirect(request.referrer or url_for("projects"))


@app.route("/projects/<int:project_id>/delete", methods=["POST"])
def project_delete(project_id: int):
    """
    Borra un proyecto Y todas sus tareas.
    - Solo POST
    - Confirmación por UI (JS confirm)
    - Borra primero tareas -> por FK ON DELETE CASCADE se limpian task_tags
    """
    proj = q1("SELECT id, name, archived FROM projects WHERE id=%s", (project_id,))
    if not proj:
        abort(404)

    try:
        prev_archived = int(proj.get("archived") or 0)

        # Enviar todas las tareas del proyecto a papelera conservando su historial/etiquetas/subtareas.
        exec_sql(
            "UPDATE tasks "
            "SET deleted_prev_archived=archived, deleted_at=NOW(), archived=1, archived_at=COALESCE(archived_at, NOW()) "
            "WHERE project_id=%s",
            (project_id,),
        )

        # Marcar para borrado remoto en Calendar las tareas del proyecto que tengan evento vinculado.
        exec_sql(
            "UPDATE tasks "
            "SET calendar_sync_state='pending_delete', calendar_local_changed_at=NOW() "
            "WHERE project_id=%s AND google_event_id IS NOT NULL",
            (project_id,),
        )

        # Enviar el proyecto a papelera.
        exec_sql(
            "UPDATE projects "
            "SET deleted_prev_archived=%s, deleted_at=NOW(), archived=1, archived_at=COALESCE(archived_at, NOW()), updated_at=NOW() "
            "WHERE id=%s",
            (prev_archived, project_id),
        )

        commit()
        flash(f"Proyecto '{proj['name']}' y sus tareas enviados a la papelera.", "ok")
    except Exception as e:
        rollback()
        flash(f"No se pudo enviar el proyecto a papelera: {e}", "error")

    next_url = safe_next_url(request.form.get("next"), "projects")
    return redirect(next_url)


@app.route("/projects/create", methods=["POST"])
def project_create():
    name = normalize_name(request.form.get("name", ""))
    desc = (request.form.get("description") or "").strip() or None
    folder_id = request.form.get("folder_id") or None
    folder_id = int(folder_id) if folder_id else None

    if not name:
        flash("El nombre del proyecto es obligatorio.", "error")
        return redirect(request.referrer or url_for("projects"))

    try:
        exec_sql(
            "INSERT INTO projects(name, description, folder_id) VALUES(%s,%s,%s)",
            (name, desc, folder_id),
        )
        commit()
        flash("Proyecto creado.", "ok")
    except Exception as e:
        rollback()
        flash(f"No se pudo crear el proyecto: {e}", "error")

    return redirect(request.referrer or url_for("projects"))

@app.route("/projects/<int:project_id>/archive", methods=["POST"])
def project_archive(project_id: int):
    try:
        exec_sql("UPDATE projects SET archived=1, archived_at=NOW(), updated_at=NOW() WHERE id=%s", (project_id,))
        commit()
        flash("Proyecto archivado.", "ok")
    except Exception as e:
        rollback()
        flash(f"No se pudo archivar: {e}", "error")
    return redirect(url_for("projects"))


@app.route("/projects/<int:project_id>/purge_tasks", methods=["POST"])
def project_purge_tasks(project_id: int):
    proj = q1("SELECT id, name FROM projects WHERE id=%s", (project_id,))
    if not proj:
        abort(404)

    try:
        exec_sql("DELETE FROM tasks WHERE project_id=%s", (project_id,))
        commit()
        flash(f"Proyecto '{proj['name']}' vaciado (tareas borradas).", "ok")
    except Exception as e:
        rollback()
        flash(f"No se pudo vaciar el proyecto: {e}", "error")

    return redirect(url_for("project_detail", project_id=project_id))
    
    
def _project_tasks_with_tags(project_id: int):
    rows = q(
        "SELECT t.id, t.title, t.location, t.notes, t.due_date, t.completed_at, t.last_completed_at, "
        "t.recurrence_rule, t.folder_id, t.created_at "
        "FROM tasks t WHERE t.project_id=%s ORDER BY t.id ASC",
        (project_id,),
    )
    ids = [r["id"] for r in rows]
    tags_map = load_tags_map(ids) if ids else {}
    # tags_map[task_id] => list of {id,name}
    return rows, tags_map


@app.route("/projects/<int:project_id>/export")
def project_export(project_id: int):
    proj = q1("SELECT id, name FROM projects WHERE id=%s", (project_id,))
    if not proj:
        abort(404)

    fmt = (request.args.get("fmt") or "csv").lower()
    rows, tags_map = _project_tasks_with_tags(project_id)

    safe_name = re.sub(r"[^A-Za-z0-9_\-]+", "_", proj["name"]).strip("_") or f"project_{project_id}"

    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)

        # CSV “estable” con fechas en ISO (YYYY-MM-DD) y datetimes ISO (YYYY-MM-DD HH:MM:SS)
        w.writerow([
            "title", "notes", "due_date", "completed_at", "last_completed_at",
            "recurrence_rule", "folder_id", "created_at", "tags"
        ])

        for t in rows:
            tag_names = [x["name"] for x in tags_map.get(t["id"], [])]
            w.writerow([
                t["title"] or "",
                t["notes"] or "",
                t["due_date"].isoformat() if t.get("due_date") else "",
                t["completed_at"].strftime("%Y-%m-%d %H:%M:%S") if t.get("completed_at") else "",
                t["last_completed_at"].strftime("%Y-%m-%d %H:%M:%S") if t.get("last_completed_at") else "",
                t["recurrence_rule"] or "",
                str(t["folder_id"]) if t.get("folder_id") is not None else "",
                t["created_at"].strftime("%Y-%m-%d %H:%M:%S") if t.get("created_at") else "",
                ",".join(tag_names),
            ])

        data = buf.getvalue().encode("utf-8")
        filename = f"{safe_name}_tasks.csv"
        return send_file(
            io.BytesIO(data),
            mimetype="text/csv; charset=utf-8",
            as_attachment=True,
            download_name=filename,
        )

    if fmt == "xml":
        root = ET.Element("project_export", attrib={"project_id": str(project_id), "project_name": proj["name"]})
        tasks_el = ET.SubElement(root, "tasks")

        for t in rows:
            task_el = ET.SubElement(tasks_el, "task")
            ET.SubElement(task_el, "title").text = t["title"] or ""
            ET.SubElement(task_el, "notes").text = t["notes"] or ""
            ET.SubElement(task_el, "due_date").text = t["due_date"].isoformat() if t.get("due_date") else ""
            ET.SubElement(task_el, "completed_at").text = t["completed_at"].strftime("%Y-%m-%d %H:%M:%S") if t.get("completed_at") else ""
            ET.SubElement(task_el, "last_completed_at").text = t["last_completed_at"].strftime("%Y-%m-%d %H:%M:%S") if t.get("last_completed_at") else ""
            ET.SubElement(task_el, "recurrence_rule").text = t["recurrence_rule"] or ""
            ET.SubElement(task_el, "folder_id").text = str(t["folder_id"]) if t.get("folder_id") is not None else ""
            ET.SubElement(task_el, "created_at").text = t["created_at"].strftime("%Y-%m-%d %H:%M:%S") if t.get("created_at") else ""

            tags_el = ET.SubElement(task_el, "tags")
            for tag in tags_map.get(t["id"], []):
                ET.SubElement(tags_el, "tag").text = tag["name"]

        xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        filename = f"{safe_name}_tasks.xml"
        return send_file(
            io.BytesIO(xml_bytes),
            mimetype="application/xml; charset=utf-8",
            as_attachment=True,
            download_name=filename,
        )

    abort(400, "Formato inválido. Usa fmt=csv o fmt=xml.")


@app.route("/projects/<int:project_id>/import", methods=["POST"])
def project_import(project_id: int):
    proj = q1("SELECT id, name, archived FROM projects WHERE id=%s", (project_id,))
    if not proj:
        abort(404)
    if proj.get("archived"):
        flash("No se puede importar en un proyecto archivado.", "error")
        return redirect(url_for("project_detail", project_id=project_id))

    f = request.files.get("import_file")
    if not f or not f.filename:
        flash("Selecciona un fichero .csv o .xml.", "error")
        return redirect(url_for("project_detail", project_id=project_id))

    filename = secure_filename(f.filename)
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""

    # Confirmación opcional (recomendable para evitar líos)
    # Aquí NO borramos nada: solo añadimos tareas.
    try:
        created = 0

        if ext == "csv":
            content = f.read().decode("utf-8")
            r = csv.DictReader(io.StringIO(content))
            for row in r:
                title = (row.get("title") or "").strip()
                if not title:
                    continue

                notes = (row.get("notes") or "").strip() or None
                due_date = _parse_date_iso(row.get("due_date") or "")
                completed_at = _parse_dt_iso(row.get("completed_at") or "")
                last_completed_at = _parse_dt_iso(row.get("last_completed_at") or "")
                recurrence_rule = (row.get("recurrence_rule") or "").strip() or None

                # Import dentro del proyecto => project_id fijo; folder_id normalmente NULL
                folder_id = (row.get("folder_id") or "").strip()
                folder_id = int(folder_id) if folder_id.isdigit() else None

                task_id = exec_sql(
                    "INSERT INTO tasks(title, notes, due_date, completed_at, last_completed_at, recurrence_rule, project_id, folder_id) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                    (title, notes, due_date, completed_at, last_completed_at, recurrence_rule, project_id, folder_id),
                )

                for tag_name in _split_tags_csv(row.get("tags") or ""):
                    tag_id = get_or_create_tag(tag_name)
                    exec_sql("INSERT IGNORE INTO task_tags(task_id, tag_id) VALUES(%s,%s)", (task_id, tag_id))

                created += 1

        elif ext == "xml":
            xml_bytes = f.read()
            root = ET.fromstring(xml_bytes)
            tasks_el = root.find("tasks")
            if tasks_el is None:
                raise ValueError("XML inválido: falta <tasks>.")

            for task_el in tasks_el.findall("task"):
                title = (task_el.findtext("title") or "").strip()
                if not title:
                    continue

                notes = (task_el.findtext("notes") or "").strip() or None
                due_date = _parse_date_iso(task_el.findtext("due_date") or "")
                completed_at = _parse_dt_iso(task_el.findtext("completed_at") or "")
                last_completed_at = _parse_dt_iso(task_el.findtext("last_completed_at") or "")
                recurrence_rule = (task_el.findtext("recurrence_rule") or "").strip() or None

                folder_id_txt = (task_el.findtext("folder_id") or "").strip()
                folder_id = int(folder_id_txt) if folder_id_txt.isdigit() else None

                task_id = exec_sql(
                    "INSERT INTO tasks(title, notes, due_date, completed_at, last_completed_at, recurrence_rule, project_id, folder_id) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                    (title, notes, due_date, completed_at, last_completed_at, recurrence_rule, project_id, folder_id),
                )

                tags_el = task_el.find("tags")
                if tags_el is not None:
                    for tag_node in tags_el.findall("tag"):
                        tag_name = (tag_node.text or "").strip()
                        if not tag_name:
                            continue
                        tag_id = get_or_create_tag(tag_name)
                        exec_sql("INSERT IGNORE INTO task_tags(task_id, tag_id) VALUES(%s,%s)", (task_id, tag_id))

                created += 1
        else:
            flash("Formato no soportado. Sube .csv o .xml.", "error")
            return redirect(url_for("project_detail", project_id=project_id))

        commit()
        flash(f"Importación completada: {created} tareas creadas.", "ok")

    except Exception as e:
        rollback()
        flash(f"No se pudo importar: {e}", "error")

    return redirect(url_for("project_detail", project_id=project_id))


@app.route("/api/projects/<int:project_id>/reorder_tasks", methods=["POST"])
def api_project_reorder_tasks(project_id: int):
    payload = request.get_json(silent=True) or {}
    task_ids_raw = payload.get("task_ids")

    if not isinstance(task_ids_raw, list) or not task_ids_raw:
        return jsonify({"ok": False, "error": "Lista de tareas inválida"}), 400

    try:
        task_ids = [int(x) for x in task_ids_raw]
    except Exception:
        return jsonify({"ok": False, "error": "IDs de tarea inválidos"}), 400

    project = q1("SELECT id FROM projects WHERE id=%s AND deleted_at IS NULL", (project_id,))
    if not project:
        return jsonify({"ok": False, "error": "Proyecto no encontrado"}), 404

    rows = q(
        "SELECT id FROM tasks "
        "WHERE project_id=%s AND completed_at IS NULL AND archived=0 AND deleted_at IS NULL",
        (project_id,),
    )
    expected_ids = {int(r["id"]) for r in rows}
    received_ids = set(task_ids)

    if expected_ids != received_ids:
        return jsonify({"ok": False, "error": "La lista no coincide con las tareas activas del proyecto"}), 400

    try:
        for pos, tid in enumerate(task_ids, start=1):
            exec_sql("UPDATE tasks SET sort_order=%s WHERE id=%s", (pos, tid))
        commit()
        return jsonify({"ok": True, "message": "Orden actualizado"})
    except Exception as e:
        rollback()
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/projects/<int:project_id>/move", methods=["POST"])
def api_project_move(project_id: int):
    payload = request.get_json(silent=True) or {}
    folder_id_raw = payload.get("folder_id")

    proj = q1("SELECT id FROM projects WHERE id=%s AND deleted_at IS NULL", (project_id,))
    if not proj:
        return jsonify({"ok": False, "error": "Proyecto no encontrado"}), 404

    try:
        folder_id = int(folder_id_raw)
    except Exception:
        return jsonify({"ok": False, "error": "Carpeta destino inválida"}), 400

    folder = q1("SELECT id FROM folders WHERE id=%s", (folder_id,))
    if not folder:
        return jsonify({"ok": False, "error": "Carpeta destino no válida"}), 400

    try:
        exec_sql("UPDATE projects SET folder_id=%s, updated_at=NOW() WHERE id=%s", (folder_id, project_id))
        commit()
        return jsonify({"ok": True, "message": "Proyecto movido"})
    except Exception as e:
        rollback()
        return jsonify({"ok": False, "error": str(e)}), 500
