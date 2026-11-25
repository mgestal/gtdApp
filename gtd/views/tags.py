"""Etiquetas: listado, detalle, edición, tipos y API de la barra lateral."""
from __future__ import annotations

import math

from flask import abort, flash, jsonify, redirect, render_template, request, url_for

from subtasks import load_subtask_counts, load_subtasks_map

from gtd.config import cfg_int, load_config, save_config
from gtd.utils import normalize_name, safe_next_url
from gtd.core import app
from gtd.db import commit, exec_sql, q, q1, rollback
from gtd.lookups import get_or_create_tag_type
from gtd.quick_entry import is_periodic_tag_name
from gtd.recurrence import recurring_effective_due_sql
from gtd.common import load_tags_map, subdb
from gtd.context import inject_sidebar_counts


@app.route("/tags/<int:tag_id>/edit", methods=["GET", "POST"])
def tag_edit(tag_id: int):
    tag = q1("SELECT tg.id, tg.name, tt.name AS type FROM tags tg LEFT JOIN TagsType tt ON tt.id=tg.type_id WHERE tg.id=%s", (tag_id,))
    if not tag:
        abort(404)

    type_rows = q(
        "SELECT name AS type FROM TagsType ORDER BY name ASC"
    )
    existing_tag_types = [r["type"] for r in type_rows]
    location = q1("SELECT latitude, longitude, radius_m FROM tag_locations WHERE tag_id=%s", (tag_id,))

    next_url = request.args.get("next") or request.form.get("next") or url_for("tags")

    if request.method == "POST":
        name = normalize_name(request.form.get("name", ""))
        selected_type = normalize_name(request.form.get("type_select", ""))
        new_type = normalize_name(request.form.get("type_new", ""))
        legacy_type = normalize_name(request.form.get("type", ""))

        if new_type:
            tag_type = new_type
        elif selected_type:
            tag_type = selected_type
        elif legacy_type:
            tag_type = legacy_type
        else:
            tag_type = None

        if not name:
            flash("El nombre es obligatorio.", "error")
            return redirect(url_for("tag_edit", tag_id=tag_id, next=next_url))

        location_values = None
        if request.form.get("location_enabled"):
            try:
                location_values = tuple(float(request.form.get(field, "")) for field in
                                        ("latitude", "longitude", "radius_m"))
                lat, lon, radius = location_values
                if not all(math.isfinite(value) for value in location_values) or not (
                    -90 <= lat <= 90 and -180 <= lon <= 180 and 0.01 <= radius <= 100000
                ):
                    raise ValueError
            except (TypeError, ValueError):
                flash("Introduce una latitud entre -90 y 90, una longitud entre -180 y 180 y un radio entre 0,01 y 100000 metros.", "error")
                return redirect(url_for("tag_edit", tag_id=tag_id, next=next_url))

        try:
            type_id = get_or_create_tag_type(tag_type) if tag_type else None
            exec_sql("UPDATE tags SET name=%s, type_id=%s WHERE id=%s", (name, type_id, tag_id))
            if location_values is not None:
                exec_sql(
                    "INSERT INTO tag_locations(tag_id, latitude, longitude, radius_m) VALUES(%s,%s,%s,%s) "
                    "ON DUPLICATE KEY UPDATE latitude=VALUES(latitude), longitude=VALUES(longitude), radius_m=VALUES(radius_m)",
                    (tag_id, *location_values),
                )
            else:
                exec_sql("DELETE FROM tag_locations WHERE tag_id=%s", (tag_id,))
            commit()
            flash("Etiqueta actualizada.", "ok")
            return redirect(next_url)
        except Exception as e:
            rollback()
            flash(f"No se pudo actualizar: {e}", "error")
            return redirect(url_for("tag_edit", tag_id=tag_id, next=next_url))

    return render_template(
        "tag_edit.html",
        tag=tag,
        next_url=next_url,
        existing_tag_types=existing_tag_types,
        location=location,
    )
    
    
    
@app.route("/tags/<int:tag_id>/delete", methods=["POST"])
def tag_delete(tag_id: int):
    tag = q1("SELECT id, name FROM tags WHERE id=%s", (tag_id,))
    if not tag:
        abort(404)

    next_url = request.form.get("next") or request.referrer or url_for("tags")

    try:
        # Quitar la etiqueta de todas las tareas
        exec_sql("DELETE FROM task_tags WHERE tag_id=%s", (tag_id,))
        # Borrar la etiqueta
        exec_sql("DELETE FROM tags WHERE id=%s", (tag_id,))
        commit()
        flash("Etiqueta borrada y eliminada de todas las tareas.", "ok")
    except Exception as e:
        rollback()
        flash(f"No se pudo borrar la etiqueta: {e}", "error")

    return redirect(next_url)
    

@app.route("/tags/create", methods=["POST"])
def tag_create():
    name = normalize_name(request.form.get("name", ""))
    selected_type = normalize_name(request.form.get("type_select", ""))
    new_type = normalize_name(request.form.get("type_new", ""))
    tag_type = new_type or selected_type or None
    next_url = safe_next_url(request.form.get("next"), "tags")

    if not name:
        flash("El nombre de la etiqueta es obligatorio.", "error")
        return redirect(next_url)

    try:
        type_id = get_or_create_tag_type(tag_type) if tag_type else None
        tag_id = exec_sql(
            "INSERT INTO tags(name, type_id) VALUES(%s, %s)",
            (name, type_id),
        )
        commit()
        flash(f"Etiqueta @{name} creada.", "ok")
    except Exception as e:
        rollback()
        if "Duplicate" in str(e) or "duplicate" in str(e).lower():
            flash(f"La etiqueta @{name} ya existe.", "error")
        else:
            flash(f"No se pudo crear la etiqueta: {e}", "error")

    return redirect(next_url)


@app.route("/tags")
def tags():
    qtxt = (request.args.get("q") or "").strip()
    type_filter = normalize_name(request.args.get("type", ""))
    type_filter_none_token = "__none__"

    per_page = cfg_int(["app", "pagination", "tags_per_page"], default=25, min_v=5, max_v=500)

    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    page = max(1, page)
    offset = (page - 1) * per_page

    params = []
    where_parts = []
    if qtxt:
        where_parts.append("LOWER(tg.name) LIKE %s")
        params.append(f"%{qtxt.lower()}%")
    if type_filter:
        if type_filter == type_filter_none_token:
            where_parts.append("tg.type_id IS NULL")
        else:
            where_parts.append("LOWER(COALESCE(ttype.name, '')) = %s")
            params.append(type_filter.lower())

    where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

    total_row = q1(
        f"SELECT COUNT(*) AS c FROM tags tg LEFT JOIN TagsType ttype ON ttype.id=tg.type_id {where}",
        tuple(params),
    )
    total = int(total_row["c"]) if total_row else 0
    pages = max(1, (total + per_page - 1) // per_page)

    if page > pages:
        page = pages
        offset = (page - 1) * per_page

    # (opcional pero útil) contador de tareas por etiqueta
    rows = q(
        "SELECT tg.id, tg.name, ttype.name AS type, COUNT(t.id) AS task_count, "
        "MAX(loc.tag_id IS NOT NULL) AS has_location "
        "FROM tags tg LEFT JOIN TagsType ttype ON ttype.id=tg.type_id "
        "LEFT JOIN tag_locations loc ON loc.tag_id=tg.id "
        "LEFT JOIN task_tags tasktag ON tasktag.tag_id=tg.id "
        "LEFT JOIN tasks t ON t.id=tasktag.task_id AND t.archived=0 AND t.deleted_at IS NULL "
        f"{where} "
        "GROUP BY tg.id, tg.name, ttype.name "
        "ORDER BY tg.name ASC, (ttype.name IS NULL) ASC, ttype.name ASC  "
        "LIMIT %s OFFSET %s",
        tuple(params + [per_page, offset]),
    )

    type_rows = q(
        "SELECT name AS type FROM TagsType ORDER BY name ASC"
    )
    tag_types = [r["type"] for r in type_rows]

    return render_template(
        "tags.html",
        rows=rows,
        qtxt=qtxt,
        type_filter=type_filter,
        type_filter_none_token=type_filter_none_token,
        tag_types=tag_types,
        page=page,
        pages=pages,
        total=total,
        per_page=per_page,
    )
    

@app.route("/tags/type")
def tag_type_tasks():
    type_filter = normalize_name(request.args.get("type", ""))
    if not type_filter:
        return redirect(url_for("tags"))
    is_none = type_filter == "__none__"
    type_label = "Sin tipo" if is_none else type_filter
    type_cond = "tg.type_id IS NULL" if is_none else "LOWER(COALESCE(tagtype.name, '')) = %s"
    type_params = () if is_none else (type_filter.lower(),)

    per_page = cfg_int(["app", "pagination", "tag_detail_per_page"], default=25, min_v=5, max_v=500)
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    page = max(page, 1)
    recurring_due_join, effective_due_expr = recurring_effective_due_sql("t")

    base_where = (
        "WHERE t.archived=0 AND t.deleted_at IS NULL "
        "AND (t.project_id IS NULL OR p.archived=0) "
        "AND EXISTS (SELECT 1 FROM task_tags tasktag JOIN tags tg ON tg.id=tasktag.tag_id "
        "LEFT JOIN TagsType tagtype ON tagtype.id=tg.type_id "
        f"WHERE tasktag.task_id=t.id AND {type_cond}) "
    )
    total_row = q1(
        "SELECT COUNT(*) AS c FROM tasks t LEFT JOIN projects p ON p.id=t.project_id " + base_where,
        type_params,
    )
    total = int(total_row["c"]) if total_row else 0
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    offset = (page - 1) * per_page

    rows = q(
        f"SELECT t.id, t.title, t.location, t.notes, {effective_due_expr} AS due_date, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.id AS folder_id, fd.name AS folder_name "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        f"{recurring_due_join}"
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        f"{base_where}"
        f"ORDER BY (t.completed_at IS NOT NULL) ASC, ({effective_due_expr} IS NULL) ASC, {effective_due_expr} ASC, t.id DESC "
        "LIMIT %s OFFSET %s",
        type_params + (per_page, offset),
    )
    task_ids = [r["id"] for r in rows]
    tags_map = load_tags_map(task_ids) if task_ids else {}

    return render_template(
        "tag_type_tasks.html",
        rows=rows,
        tags_map=tags_map,
        type_filter=type_filter,
        type_label=type_label,
        page=page,
        pages=pages,
        total=total,
        per_page=per_page,
    )


@app.route("/tags/<int:tag_id>")
def tag_detail(tag_id: int):
    tag = q1("SELECT id, name FROM tags WHERE id=%s", (tag_id,))
    if not tag:
        abort(404)

    per_page = cfg_int(["app", "pagination", "tag_detail_per_page"], default=25, min_v=5, max_v=500)
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    page = max(page, 1)
    offset = (page - 1) * per_page
    recurring_due_join, effective_due_expr = recurring_effective_due_sql("t")
    tag_sidebar_cfg = (load_config().get("sidebar") or {}).get("tag_task_visibility") or {}
    include_completed = bool(tag_sidebar_cfg.get("completed", False))
    include_archived = bool(tag_sidebar_cfg.get("archived", False))
    location_pending = request.args.get("location_pending") == "1"
    if location_pending:
        include_completed = False
        include_archived = False
    completed_clause = "" if include_completed else "AND t.completed_at IS NULL "
    archived_clause = "" if include_archived else "AND t.archived=0 "
    project_archived_clause = "" if include_archived else "AND (t.project_id IS NULL OR p.archived=0) "
    project_deleted_clause = "AND (t.project_id IS NULL OR p.deleted_at IS NULL) " if location_pending else ""

    # total de tareas con esa etiqueta
    total_row = q1(
        "SELECT COUNT(*) AS c "
        "FROM task_tags tt "
        "JOIN tasks t ON t.id=tt.task_id "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE tt.tag_id=%s "
        f"{archived_clause}"
        "AND t.deleted_at IS NULL "
        f"{project_archived_clause}"
        f"{project_deleted_clause}"
        f"{completed_clause}",
        (tag_id,),
    )
    total = int(total_row["c"]) if total_row else 0
    pages = max(1, (total + per_page - 1) // per_page)

    # tareas (con proyecto para mostrarlo/enlazarlo)
    rows = q(
        f"SELECT t.id, t.title, t.location, t.notes, {effective_due_expr} AS due_date, "
        "TIME_FORMAT(t.due_time, '%%H:%%i') AS due_time_text, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.id AS folder_id, fd.name AS folder_name "
        "FROM task_tags tt "
        "JOIN tasks t ON t.id=tt.task_id "
        "LEFT JOIN projects p ON p.id=t.project_id "
        f"{recurring_due_join}"
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "WHERE tt.tag_id=%s "
        f"{archived_clause}"
        "AND t.deleted_at IS NULL "
        f"{project_archived_clause}"
        f"{project_deleted_clause}"
        f"{completed_clause}"
        f"ORDER BY (t.completed_at IS NOT NULL) ASC, ({effective_due_expr} IS NULL) ASC, {effective_due_expr} ASC, t.id DESC "
        "LIMIT %s OFFSET %s",
        (tag_id, per_page, offset),
    )

    task_ids = [r["id"] for r in rows]
    tags_map = load_tags_map(task_ids) if task_ids else {}
    sub_counts = load_subtask_counts(subdb, task_ids)
    sub_map = load_subtasks_map(subdb, task_ids)

    return render_template(
        "tag_detail.html",
        tag=tag,
        rows=rows,
        tags_map=tags_map,
        page=page,
        pages=pages,
        total=total,
        per_page=per_page,
        periodic_tag=is_periodic_tag_name(tag.get("name") or ""),
        periodic_history_url=url_for("periodic_history", tag_id=tag_id),
        location_pending=location_pending,
    )


@app.route("/api/sidebar/filters/order", methods=["POST"])
def api_sidebar_filters_order():
    payload = request.get_json(silent=True) or {}
    order = payload.get("order")
    if not isinstance(order, list) or any(not isinstance(i, int) for i in order):
        return jsonify({"ok": False, "error": "Orden no válido"}), 400
    valid_ids = {int(r["id"]) for r in (q("SELECT id FROM filters") or [])}
    order = [i for i in dict.fromkeys(order) if i in valid_ids]
    cfg = load_config()
    cfg.setdefault("sidebar", {})["filter_order"] = order
    save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/sidebar/tags/order", methods=["POST"])
def api_sidebar_tags_order():
    payload = request.get_json(silent=True) or {}
    order = payload.get("order")
    if not isinstance(order, list) or any(not isinstance(i, int) for i in order):
        return jsonify({"ok": False, "error": "Orden no válido"}), 400
    valid_ids = {int(r["id"]) for r in (q("SELECT id FROM tags") or [])}
    order = [i for i in dict.fromkeys(order) if i in valid_ids]
    cfg = load_config()
    cfg.setdefault("sidebar", {})["tag_order"] = order
    save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/sidebar/tags/task-visibility", methods=["POST"])
def api_sidebar_tags_task_visibility():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload.get("completed"), bool) or not isinstance(payload.get("archived"), bool):
        return jsonify({"ok": False, "error": "Opciones no válidas"}), 400
    cfg = load_config()
    cfg.setdefault("sidebar", {})["tag_task_visibility"] = {
        "completed": payload["completed"],
        "archived": payload["archived"],
    }
    save_config(cfg)
    return jsonify({"ok": True})


@app.route("/api/sidebar/tags/<int:tag_id>/type", methods=["POST"])
def api_sidebar_tag_type(tag_id: int):
    payload = request.get_json(silent=True) or {}
    raw_type = payload.get("type")
    if raw_type is not None and not isinstance(raw_type, str):
        return jsonify({"ok": False, "error": "Tipo no válido"}), 400
    tag_type = normalize_name(raw_type or "") or None
    if not q1("SELECT id FROM tags WHERE id=%s", (tag_id,)):
        return jsonify({"ok": False, "error": "Etiqueta no encontrada"}), 404
    try:
        type_id = get_or_create_tag_type(tag_type) if tag_type else None
        exec_sql("UPDATE tags SET type_id=%s WHERE id=%s", (type_id, tag_id))
        commit()
    except Exception as e:
        rollback()
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/tasks/<int:task_id>/add_tag", methods=["POST"])
def api_task_add_tag(task_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        tag_id = int(payload.get("tag_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Etiqueta no válida"}), 400
    if not q1("SELECT id FROM tasks WHERE id=%s AND deleted_at IS NULL", (task_id,)):
        return jsonify({"ok": False, "error": "Tarea no encontrada"}), 404
    tag = q1("SELECT id, name FROM tags WHERE id=%s", (tag_id,))
    if not tag:
        return jsonify({"ok": False, "error": "Etiqueta no encontrada"}), 404
    try:
        exec_sql("INSERT IGNORE INTO task_tags(task_id, tag_id) VALUES(%s,%s)", (task_id, tag_id))
        commit()
    except Exception as e:
        rollback()
        return jsonify({"ok": False, "error": str(e)}), 500
    # Datos para pintar la etiqueta en la fila sin recargar (live_update.js)
    return jsonify({
        "ok": True,
        "tag": {"id": tag["id"], "name": tag["name"], "url": url_for("tag_detail", tag_id=tag["id"])},
        "counts": inject_sidebar_counts()["sidebar_counts"],
    })


@app.route("/api/tasks/<int:task_id>/remove_tag", methods=["POST"])
def api_task_remove_tag(task_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        tag_id = int(payload.get("tag_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Etiqueta no válida"}), 400
    if not q1("SELECT id FROM tasks WHERE id=%s AND deleted_at IS NULL", (task_id,)):
        return jsonify({"ok": False, "error": "Tarea no encontrada"}), 404
    try:
        exec_sql("DELETE FROM task_tags WHERE task_id=%s AND tag_id=%s", (task_id, tag_id))
        commit()
    except Exception as e:
        rollback()
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})
