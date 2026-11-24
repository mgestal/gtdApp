"""Búsqueda multitipo y API de autocompletado."""
from __future__ import annotations

from flask import jsonify, render_template, request

from gtd.config import cfg_int
from gtd.core import app
from gtd.db import q, q1
from gtd.recurrence import recurring_effective_due_sql
from gtd.common import admin_required, load_tags_map


@app.route("/search")
def search():
    qtxt = (request.args.get("q") or "").strip()
    search_type = request.args.get("type", "tasks")
    if search_type not in ("tasks", "projects", "folders", "tags", "filters", "archive", "trash"):
        search_type = "tasks"
    can_search_trash = admin_required()

    per_page = cfg_int(["app", "pagination", "search_per_page"], default=25, min_v=5, max_v=500)

    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    page = max(page, 1)
    offset = (page - 1) * per_page

    total = 0
    pages = 1
    rows = []
    tags_map = {}
    recurring_due_join, effective_due_expr = recurring_effective_due_sql("t")

    if qtxt:
        like = f"%{qtxt.lower()}%"

        if search_type == "tasks":
            # FULLTEXT (MATCH…AGAINST) requiere mínimo 3 caracteres en MySQL por defecto.
            # Para términos cortos (< 3 chars) usamos LIKE para que "CV", "IO", etc. funcionen.
            use_fulltext = len(qtxt) >= 3
            if use_fulltext:
                total_row = q1(
                    "SELECT COUNT(*) AS c FROM tasks t "
                    "WHERE t.deleted_at IS NULL "
                    "AND MATCH(t.title, t.notes) AGAINST(%s IN BOOLEAN MODE)",
                    (qtxt + "*",),
                )
            else:
                total_row = q1(
                    "SELECT COUNT(*) AS c FROM tasks t "
                    "WHERE t.deleted_at IS NULL "
                    "AND (LOWER(t.title) LIKE %s OR LOWER(t.notes) LIKE %s)",
                    (like, like),
                )
            total = int(total_row["c"]) if total_row else 0
            pages = max(1, (total + per_page - 1) // per_page)
            page = min(page, pages)
            offset = (page - 1) * per_page
            _task_select = (
                f"SELECT t.id, t.title, t.location, t.notes, {effective_due_expr} AS due_date, t.completed_at, t.recurrence_rule, t.archived, t.archived_at, "
                "p.name AS project_name, p.id AS project_id, "
                "fd.id AS folder_id, fd.name AS folder_name "
                "FROM tasks t "
                "LEFT JOIN projects p ON p.id=t.project_id "
                f"{recurring_due_join}"
                "LEFT JOIN folders fd ON fd.id = COALESCE(t.folder_id, p.folder_id) "
                "WHERE t.deleted_at IS NULL "
            )
            _task_order = (
                "ORDER BY t.archived ASC, (t.completed_at IS NOT NULL) ASC, "
                f"({effective_due_expr} IS NULL) ASC, {effective_due_expr} ASC, t.id DESC "
                "LIMIT %s OFFSET %s"
            )
            if use_fulltext:
                rows = q(
                    _task_select +
                    "AND MATCH(t.title, t.notes) AGAINST(%s IN BOOLEAN MODE) " +
                    _task_order,
                    (qtxt + "*", per_page, offset),
                )
            else:
                rows = q(
                    _task_select +
                    "AND (LOWER(t.title) LIKE %s OR LOWER(t.notes) LIKE %s) " +
                    _task_order,
                    (like, like, per_page, offset),
                )
            tags_map = load_tags_map([r["id"] for r in rows]) if rows else {}

        elif search_type == "projects":
            total_row = q1(
                "SELECT COUNT(*) AS c FROM projects "
                "WHERE deleted_at IS NULL "
                "AND (LOWER(name) LIKE %s OR LOWER(description) LIKE %s)",
                (like, like),
            )
            total = int(total_row["c"]) if total_row else 0
            pages = max(1, (total + per_page - 1) // per_page)
            page = min(page, pages)
            offset = (page - 1) * per_page
            rows = q(
                "SELECT p.id, p.name, p.description, p.archived, "
                "f.name AS folder_name, f.id AS folder_id, "
                "(SELECT COUNT(*) FROM tasks t WHERE t.project_id=p.id AND t.completed_at IS NULL AND t.archived=0 AND t.deleted_at IS NULL) AS pending "
                "FROM projects p LEFT JOIN folders f ON f.id=p.folder_id "
                "WHERE p.deleted_at IS NULL "
                "AND (LOWER(p.name) LIKE %s OR LOWER(p.description) LIKE %s) "
                "ORDER BY p.archived ASC, p.name ASC "
                "LIMIT %s OFFSET %s",
                (like, like, per_page, offset),
            )

        elif search_type == "folders":
            total_row = q1(
                "SELECT COUNT(*) AS c FROM folders WHERE LOWER(name) LIKE %s",
                (like,),
            )
            total = int(total_row["c"]) if total_row else 0
            pages = max(1, (total + per_page - 1) // per_page)
            page = min(page, pages)
            offset = (page - 1) * per_page
            rows = q(
                "SELECT f.id, f.name, p.name AS parent_name, p.id AS parent_id, "
                "(SELECT COUNT(*) FROM projects pr WHERE pr.folder_id=f.id AND pr.archived=0) AS project_count "
                "FROM folders f LEFT JOIN folders p ON p.id=f.parent_id "
                "WHERE LOWER(f.name) LIKE %s "
                "ORDER BY f.name ASC "
                "LIMIT %s OFFSET %s",
                (like, per_page, offset),
            )

        elif search_type == "tags":
            total_row = q1(
                "SELECT COUNT(*) AS c FROM tags WHERE LOWER(name) LIKE %s",
                (like,),
            )
            total = int(total_row["c"]) if total_row else 0
            pages = max(1, (total + per_page - 1) // per_page)
            page = min(page, pages)
            offset = (page - 1) * per_page
            rows = q(
                "SELECT tg.id, tg.name, ttype.name AS type, "
                "(SELECT COUNT(*) FROM task_tags tasktag JOIN tasks t ON t.id=tasktag.task_id WHERE tasktag.tag_id=tg.id AND t.archived=0 AND t.deleted_at IS NULL) AS task_count "
                "FROM tags tg LEFT JOIN TagsType ttype ON ttype.id=tg.type_id "
                "WHERE LOWER(tg.name) LIKE %s "
                "ORDER BY tg.name ASC "
                "LIMIT %s OFFSET %s",
                (like, per_page, offset),
            )

        elif search_type == "filters":
            total_row = q1(
                "SELECT COUNT(*) AS c FROM filters WHERE LOWER(name) LIKE %s OR LOWER(expression) LIKE %s",
                (like, like),
            )
            total = int(total_row["c"]) if total_row else 0
            pages = max(1, (total + per_page - 1) // per_page)
            page = min(page, pages)
            offset = (page - 1) * per_page
            rows = q(
                "SELECT id, name, expression FROM filters "
                "WHERE LOWER(name) LIKE %s OR LOWER(expression) LIKE %s "
                "ORDER BY name ASC "
                "LIMIT %s OFFSET %s",
                (like, like, per_page, offset),
            )

        elif search_type == "archive":
            total_row = q1(
                "SELECT ("
                "  SELECT COUNT(*) FROM tasks t "
                "  WHERE t.archived=1 AND t.deleted_at IS NULL "
                "  AND (LOWER(t.title) LIKE %s OR LOWER(COALESCE(t.location, t.notes, '')) LIKE %s)"
                ") + ("
                "  SELECT COUNT(*) FROM projects p "
                "  WHERE p.archived=1 AND p.deleted_at IS NULL "
                "  AND (LOWER(p.name) LIKE %s OR LOWER(COALESCE(p.description, '')) LIKE %s)"
                ") AS c",
                (like, like, like, like),
            )
            total = int(total_row["c"]) if total_row else 0
            pages = max(1, (total + per_page - 1) // per_page)
            page = min(page, pages)
            offset = (page - 1) * per_page
            rows = q(
                "SELECT * FROM ("
                "  SELECT 'task' AS item_type, t.id, t.title, t.notes AS details, t.archived_at AS state_at, t.completed_at, "
                "         p.id AS project_id, p.name AS project_name, fd.id AS folder_id, fd.name AS folder_name "
                "  FROM tasks t "
                "  LEFT JOIN projects p ON p.id=t.project_id "
                "  LEFT JOIN folders fd ON fd.id=COALESCE(t.folder_id, p.folder_id) "
                "  WHERE t.archived=1 AND t.deleted_at IS NULL "
                "    AND (LOWER(t.title) LIKE %s OR LOWER(COALESCE(t.location, t.notes, '')) LIKE %s) "
                "  UNION ALL "
                "  SELECT 'project' AS item_type, p.id, p.name AS title, p.description AS details, p.archived_at AS state_at, NULL AS completed_at, "
                "         p.id AS project_id, p.name AS project_name, f.id AS folder_id, f.name AS folder_name "
                "  FROM projects p "
                "  LEFT JOIN folders f ON f.id=p.folder_id "
                "  WHERE p.archived=1 AND p.deleted_at IS NULL "
                "    AND (LOWER(p.name) LIKE %s OR LOWER(COALESCE(p.description, '')) LIKE %s) "
                ") archive_search "
                "ORDER BY (state_at IS NULL) ASC, state_at DESC, title ASC "
                "LIMIT %s OFFSET %s",
                (like, like, like, like, per_page, offset),
            )

        elif search_type == "trash":
            if can_search_trash:
                total_row = q1(
                    "SELECT ("
                    "  SELECT COUNT(*) FROM tasks t "
                    "  WHERE t.deleted_at IS NOT NULL "
                    "  AND (LOWER(t.title) LIKE %s OR LOWER(COALESCE(t.location, t.notes, '')) LIKE %s)"
                    ") + ("
                    "  SELECT COUNT(*) FROM projects p "
                    "  WHERE p.deleted_at IS NOT NULL "
                    "  AND (LOWER(p.name) LIKE %s OR LOWER(COALESCE(p.description, '')) LIKE %s)"
                    ") AS c",
                    (like, like, like, like),
                )
                total = int(total_row["c"]) if total_row else 0
                pages = max(1, (total + per_page - 1) // per_page)
                page = min(page, pages)
                offset = (page - 1) * per_page
                rows = q(
                    "SELECT * FROM ("
                    "  SELECT 'task' AS item_type, t.id, t.title, t.notes AS details, t.deleted_at AS state_at, NULL AS completed_at, "
                    "         p.id AS project_id, p.name AS project_name, fd.id AS folder_id, fd.name AS folder_name "
                    "  FROM tasks t "
                    "  LEFT JOIN projects p ON p.id=t.project_id "
                    "  LEFT JOIN folders fd ON fd.id=COALESCE(t.folder_id, p.folder_id) "
                    "  WHERE t.deleted_at IS NOT NULL "
                    "    AND (LOWER(t.title) LIKE %s OR LOWER(COALESCE(t.location, t.notes, '')) LIKE %s) "
                    "  UNION ALL "
                    "  SELECT 'project' AS item_type, p.id, p.name AS title, p.description AS details, p.deleted_at AS state_at, NULL AS completed_at, "
                    "         p.id AS project_id, p.name AS project_name, f.id AS folder_id, f.name AS folder_name "
                    "  FROM projects p "
                    "  LEFT JOIN folders f ON f.id=p.folder_id "
                    "  WHERE p.deleted_at IS NOT NULL "
                    "    AND (LOWER(p.name) LIKE %s OR LOWER(COALESCE(p.description, '')) LIKE %s) "
                    ") trash_search "
                    "ORDER BY (state_at IS NULL) ASC, state_at DESC, title ASC "
                    "LIMIT %s OFFSET %s",
                    (like, like, like, like, per_page, offset),
                )

    return render_template(
        "search.html",
        qtxt=qtxt,
        search_type=search_type,
        rows=rows,
        tags_map=tags_map,
        can_search_trash=can_search_trash,
        page=page,
        pages=pages,
        total=total,
        per_page=per_page,
    )


@app.route("/api/tags/search")
def api_tags_search():
    qtxt = (request.args.get("q") or "").strip().lower()

    if not qtxt:
        return jsonify({"items": []})

    qtxt = qtxt[:50]

    rows = q(
        "SELECT tg.id, tg.name, tg.type_id, tt.name AS type "
        "FROM tags tg LEFT JOIN TagsType tt ON tt.id=tg.type_id "
        "WHERE LOWER(tg.name) LIKE %s "
        "ORDER BY tg.name "
        "LIMIT 8",
        (f"%{qtxt}%",),
    )

    return jsonify({"items": rows})


@app.route("/api/projects/search")
def api_projects_search():
    qtxt = (request.args.get("q") or "").strip().lower()

    if not qtxt:
        rows = q(
            "SELECT id, name FROM projects WHERE archived=0 AND deleted_at IS NULL ORDER BY name LIMIT 8"
        )
        return jsonify({"items": rows})

    qtxt = qtxt[:50]

    rows = q(
        "SELECT id, name "
        "FROM projects "
        "WHERE archived=0 AND deleted_at IS NULL AND LOWER(name) LIKE %s "
        "ORDER BY name "
        "LIMIT 8",
        (f"%{qtxt}%",),
    )

    return jsonify({"items": rows})


@app.route("/api/folders/search")
def api_folders_search():
    qtxt = (request.args.get("q") or "").strip().lower()

    if not qtxt:
        rows = q(
            "SELECT id, name FROM folders ORDER BY name LIMIT 8"
        )
        return jsonify({"items": rows})

    qtxt = qtxt[:50]

    rows = q(
        "SELECT id, name "
        "FROM folders "
        "WHERE LOWER(name) LIKE %s "
        "ORDER BY name "
        "LIMIT 8",
        (f"%{qtxt}%",),
    )

    return jsonify({"items": rows})
