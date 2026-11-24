"""Filtros guardados y ejecución de expresiones."""
from __future__ import annotations

from flask import abort, flash, redirect, render_template, request, url_for

from subtasks import load_subtask_counts, load_subtasks_map

from gtd.config import cfg_int
from gtd.utils import normalize_name
from gtd.core import app
from gtd.db import commit, exec_sql, q, q1, rollback
from gtd.filter_expr import (
    And,
    ast_contains_done,
    compile_filter_to_sql,
    Not,
    parse_filter_expression,
    Term,
)
from gtd.recurrence import recurring_effective_due_sql
from gtd.common import load_tags_map, subdb


# ---------------- Filters ----------------
@app.route("/filters", methods=["GET", "POST"])
def filters_view():
    if request.method == "POST":
        name = normalize_name(request.form.get("name", ""))
        expr = (request.form.get("expression") or "").strip()

        if not name or not expr:
            flash("Nombre y expresión son obligatorios.", "error")
            return redirect(url_for("filters_view"))

        # Validar parseo
        try:
            _ = parse_filter_expression(expr)
        except Exception as e:
            flash(f"Expresión inválida: {e}", "error")
            return redirect(url_for("filters_view"))

        try:
            exec_sql("INSERT INTO filters(name, expression) VALUES(%s,%s)", (name, expr))
            commit()
            flash("Filtro creado.", "ok")
        except Exception as e:
            rollback()
            flash(f"No se pudo crear el filtro: {e}", "error")

        return redirect(url_for("filters_view"))

    qtxt = (request.args.get("q") or "").strip()

    per_page = cfg_int(["app", "pagination", "filters_per_page"], default=25, min_v=5, max_v=500)

    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    page = max(1, page)
    offset = (page - 1) * per_page

    params = []
    where = ""
    if qtxt:
        where = "WHERE LOWER(name) LIKE %s OR LOWER(expression) LIKE %s"
        like = f"%{qtxt.lower()}%"
        params.extend([like, like])

    total_row = q1(
        f"SELECT COUNT(*) AS c FROM filters {where}",
        tuple(params),
    )
    total = int(total_row["c"]) if total_row else 0
    pages = max(1, (total + per_page - 1) // per_page)

    if page > pages:
        page = pages
        offset = (page - 1) * per_page

    rows = q(
        f"SELECT id, name, expression "
        f"FROM filters "
        f"{where} "
        f"ORDER BY name "
        f"LIMIT %s OFFSET %s",
        tuple(params + [per_page, offset]),
    )

    return render_template(
        "filters.html",
        rows=rows,
        qtxt=qtxt,
        page=page,
        pages=pages,
        total=total,
        per_page=per_page,
    )

@app.route("/filters/<int:filter_id>")
def filter_run(filter_id: int):
    flt = q1("SELECT id, name, expression FROM filters WHERE id=%s", (filter_id,))
    if not flt:
        abort(404)

    # paginación
    per_page = cfg_int(["app", "pagination", "filter_per_page"], default=25, min_v=5, max_v=500)
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    page = max(page, 1)
    offset = (page - 1) * per_page
    recurring_due_join, effective_due_expr = recurring_effective_due_sql("t")

    try:
        ast = parse_filter_expression(flt["expression"])

        # Por defecto: solo tareas abiertas
        if not ast_contains_done(ast):
            ast = And(ast, Not(Term("IDENT", "done")))

        where_sql, params = compile_filter_to_sql(ast)
        where_sql_effective = where_sql.replace("t.due_date", effective_due_expr)
    except Exception as e:
        flash(f"Error en el filtro '{flt['name']}': {e}", "error")
        return redirect(url_for("filters_view"))

    # Total para paginar (misma condición WHERE)
    total_row = q1(
        "SELECT COUNT(*) AS c "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "LEFT JOIN folders pf ON pf.id=p.folder_id "
        f"{recurring_due_join}"
        f"WHERE {where_sql_effective} AND t.archived=0 AND t.deleted_at IS NULL AND (t.project_id IS NULL OR p.archived = 0)",
        tuple(params),
    )
    total = int(total_row["c"]) if total_row else 0
    pages = max(1, (total + per_page - 1) // per_page)

    # Página de resultados
    sql = (
        f"SELECT t.id, t.title, t.location, t.notes, {effective_due_expr} AS due_date, t.due_time, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        f"{recurring_due_join}"
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "LEFT JOIN folders pf ON pf.id=p.folder_id "
        f"WHERE {where_sql_effective} AND t.archived=0 AND t.deleted_at IS NULL AND (t.project_id IS NULL OR p.archived = 0) "
        f"ORDER BY (t.completed_at IS NOT NULL) ASC, ({effective_due_expr} IS NULL) ASC, {effective_due_expr} ASC, t.id DESC "
        "LIMIT %s OFFSET %s"
    )

    rows = q(sql, tuple(params) + (per_page, offset))
    task_ids = [r["id"] for r in rows]
    tags_map = load_tags_map(task_ids) if task_ids else {}
    sub_counts = load_subtask_counts(subdb, task_ids)
    sub_map = load_subtasks_map(subdb, task_ids)

    return render_template(
        "filter_detail.html",
        flt=flt,
        rows=rows,
        tags_map=tags_map,
        page=page,
        pages=pages,
        total=total,
        per_page=per_page,
    )


@app.route("/filters/<int:filter_id>/edit", methods=["GET", "POST"])
def filter_edit(filter_id: int):
    flt = q1("SELECT id, name, expression FROM filters WHERE id=%s", (filter_id,))
    if not flt:
        abort(404)

    if request.method == "POST":
        name = normalize_name(request.form.get("name", ""))
        expr = (request.form.get("expression") or "").strip()

        if not name or not expr:
            flash("Nombre y expresión son obligatorios.", "error")
            return redirect(url_for("filter_edit", filter_id=filter_id))

        # Validación de expresión
        try:
            _ = parse_filter_expression(expr)
        except Exception as e:
            flash(f"Expresión inválida: {e}", "error")
            return redirect(url_for("filter_edit", filter_id=filter_id))

        try:
            exec_sql(
                "UPDATE filters SET name=%s, expression=%s WHERE id=%s",
                (name, expr, filter_id),
            )
            commit()
            flash("Filtro actualizado.", "ok")
            return redirect(url_for("filters_view"))
        except Exception as e:
            rollback()
            # si el error es por UNIQUE name duplicado, lo mostramos claro
            msg = str(e)
            if "Duplicate" in msg or "duplicate" in msg:
                flash("Ya existe un filtro con ese nombre.", "error")
            else:
                flash(f"No se pudo actualizar: {e}", "error")
            return redirect(url_for("filter_edit", filter_id=filter_id))

    return render_template("filter_edit.html", flt=flt)


@app.route("/filters/<int:filter_id>/delete", methods=["POST"])
def filter_delete(filter_id: int):
    try:
        exec_sql("DELETE FROM filters WHERE id=%s", (filter_id,))
        commit()
        flash("Filtro borrado.", "ok")
    except Exception as e:
        rollback()
        flash(f"No se pudo borrar: {e}", "error")
    return redirect(url_for("filters_view"))


@app.route("/filters/run")
def filter_run_expression():

    expr = (request.args.get("expr") or "").strip()

    if not expr:
        flash("Debes escribir una expresión.", "error")
        return redirect(url_for("filters_view"))

    per_page = cfg_int(["app", "pagination", "filters_per_page"], default=25, min_v=5, max_v=500)

    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1

    page = max(page, 1)
    offset = (page - 1) * per_page
    recurring_due_join, effective_due_expr = recurring_effective_due_sql("t")

    try:
        ast = parse_filter_expression(expr)

        # Por defecto: solo tareas abiertas
        if not ast_contains_done(ast):
            ast = And(ast, Not(Term("IDENT", "done")))

        where_sql, params = compile_filter_to_sql(ast)
        where_sql_effective = where_sql.replace("t.due_date", effective_due_expr)
    except Exception as e:
        flash(f"Expresión inválida: {e}", "error")
        return redirect(url_for("filters_view"))

    total_row = q1(
        "SELECT COUNT(*) AS c "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "LEFT JOIN folders pf ON pf.id=p.folder_id "
        f"{recurring_due_join}"
        f"WHERE {where_sql_effective} AND t.archived=0 AND t.deleted_at IS NULL AND (t.project_id IS NULL OR p.archived = 0)",
        tuple(params),
    )

    total = int(total_row["c"]) if total_row else 0
    pages = max(1, (total + per_page - 1) // per_page)

    sql = (
        f"SELECT t.id, t.title, t.location, t.notes, {effective_due_expr} AS due_date, t.completed_at, t.recurrence_rule, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        f"{recurring_due_join}"
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "LEFT JOIN folders pf ON pf.id=p.folder_id "
        f"WHERE {where_sql_effective} AND t.archived=0 AND t.deleted_at IS NULL AND (t.project_id IS NULL OR p.archived = 0) "
        f"ORDER BY (t.completed_at IS NOT NULL) ASC, ({effective_due_expr} IS NULL) ASC, {effective_due_expr} ASC, t.id DESC "
        "LIMIT %s OFFSET %s"
    )

    rows = q(sql, tuple(params) + (per_page, offset))

    task_ids = [r["id"] for r in rows]
    tags_map = load_tags_map(task_ids) if task_ids else {}
    sub_counts = load_subtask_counts(subdb, task_ids)
    sub_map = load_subtasks_map(subdb, task_ids)

    return render_template(
        "filter_expression.html",
        expr=expr,
        rows=rows,
        tags_map=tags_map,
        page=page,
        pages=pages,
        total=total,
        per_page=per_page,
    )
