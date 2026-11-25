"""Carpetas: listado, detalle, crear, renombrar, mover, borrar y purgas."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from flask import abort, flash, jsonify, redirect, render_template, request, url_for

from gtd.config import cfg_int
from gtd.utils import normalize_name
from gtd.core import app
from gtd.db import commit, exec_sql, q, q1, rollback
from gtd.roles import get_folder_tree_ids, role_tag_sql_names
from gtd.recurrence import recurring_effective_due_sql
from gtd.common import build_folder_breadcrumb, load_tags_map


@app.route("/folders")
def folders():
    qtxt = (request.args.get("q") or "").strip()

    per_page = cfg_int(["app", "pagination", "folders_per_page"], default=25, min_v=5, max_v=500)
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    page = max(1, page)
    offset = (page - 1) * per_page

    params = []
    where = ""
    if qtxt:
        # Filtro por “contiene” (subcadena). LOWER para hacerlo insensible a mayúsculas.
        where = "WHERE LOWER(name) LIKE %s"
        params.append(f"%{qtxt.lower()}%")

    total_row = q1(
        f"SELECT COUNT(*) AS c FROM folders {where}",
        tuple(params),
    )
    total = int(total_row["c"]) if total_row else 0
    pages = max(1, (total + per_page - 1) // per_page)

    # Si el usuario pide una page fuera de rango tras filtrar, volver a la última válida
    if pages > 0 and page > pages:
        page = pages
        offset = (page - 1) * per_page

    rows = q(
        f"SELECT id, name, parent_id FROM folders {where} "
        "ORDER BY name "
        "LIMIT %s OFFSET %s",
        tuple(params + [per_page, offset]),
    )

    return render_template(
        "folders.html",
        rows=rows,
        qtxt=qtxt,
        page=page,
        pages=pages,
        total=total,
        per_page=per_page,
    )


    

#@app.route("/folders")
#def folders_view():
#    folders = q("SELECT id, parent_id, name FROM folders ORDER BY name")
#    return render_template("folders.html", folders=folders)


@app.route("/folders/create", methods=["POST"])
def folder_create():
    name = normalize_name(request.form.get("name", ""))
    parent_id = request.form.get("parent_id") or None
    parent_id = int(parent_id) if parent_id else None

    if not name:
        flash("El nombre de la carpeta es obligatorio.", "error")
        return redirect(request.referrer or url_for("folders_view"))

    try:
        exec_sql("INSERT INTO folders(name, parent_id) VALUES(%s,%s)", (name, parent_id))
        commit()
        flash("Carpeta creada.", "ok")
    except Exception as e:
        rollback()
        flash(f"No se pudo crear la carpeta: {e}", "error")

    return redirect(request.referrer or url_for("folders_view"))


@app.route("/folders/<int:folder_id>")
def folder_detail(folder_id: int):
    folder = q1("SELECT id, name, parent_id FROM folders WHERE id=%s", (folder_id,))
    if not folder:
        abort(404)
    recurring_due_join, effective_due_expr = recurring_effective_due_sql("t")

    folder_breadcrumb = build_folder_breadcrumb(folder_id, include_self=False)

    include_subfolders = (request.args.get("subfolders") or "").strip() in ("1", "true", "yes")

    # Proyectos dentro de la carpeta (activos)
    projects = q(
        "SELECT id, name FROM projects WHERE folder_id=%s AND archived=0 AND deleted_at IS NULL ORDER BY name",
        (folder_id,)
    )

    tasks_grouped: List[Dict[str, Any]] = []

    if include_subfolders:
        folder_ids = sorted(get_folder_tree_ids(folder_id, include_self=True))
        placeholders_f = ",".join(["%s"] * len(folder_ids))

        subtree_projects = q(
            f"SELECT id FROM projects WHERE folder_id IN ({placeholders_f}) "
            "AND archived=0 AND deleted_at IS NULL",
            tuple(folder_ids),
        )
        project_ids = [p["id"] for p in subtree_projects]

        where_scope = f"(t.folder_id IN ({placeholders_f}) AND t.project_id IS NULL)"
        params: List[Any] = list(folder_ids)
        if project_ids:
            placeholders_p = ",".join(["%s"] * len(project_ids))
            where_scope += f" OR t.project_id IN ({placeholders_p})"
            params += project_ids

        # Tareas asignadas a la carpeta, subcarpetas o proyectos contenidos en ellas
        tasks = q(
            f"SELECT t.id, t.title, t.location, t.notes, {effective_due_expr} AS due_date, t.due_time, "
            "t.completed_at, t.recurrence_rule, t.priority, t.folder_id, t.project_id, "
            "COALESCE(tf.name, pf.name) AS group_folder_name, "
            "COALESCE(t.folder_id, p.folder_id) AS group_folder_id, "
            "p.name AS project_name "
            "FROM tasks t "
            "LEFT JOIN folders tf ON tf.id=t.folder_id "
            "LEFT JOIN projects p ON p.id=t.project_id "
            "LEFT JOIN folders pf ON pf.id=p.folder_id "
            f"{recurring_due_join}"
            f"WHERE ({where_scope}) "
            "AND t.archived=0 AND t.deleted_at IS NULL AND t.completed_at IS NULL "
            "ORDER BY group_folder_name ASC, (p.name IS NULL) DESC, p.name ASC, "
            f"({effective_due_expr} IS NULL) ASC, {effective_due_expr} ASC, t.id DESC",
            tuple(params),
        )

        groups: Dict[Any, Dict[str, Any]] = {}
        group_order: List[Any] = []
        for t in tasks:
            fid = t["group_folder_id"]
            if fid not in groups:
                groups[fid] = {
                    "folder_id": fid,
                    "folder_name": t["group_folder_name"],
                    "projects": {},
                    "project_order": [],
                }
                group_order.append(fid)
            g = groups[fid]
            pid = t["project_id"]
            if pid not in g["projects"]:
                g["projects"][pid] = {"project_id": pid, "project_name": t["project_name"], "tasks": []}
                g["project_order"].append(pid)
            g["projects"][pid]["tasks"].append(t)

        for fid in group_order:
            g = groups[fid]
            g["projects"] = [g["projects"][pid] for pid in g["project_order"]]
            tasks_grouped.append(g)
    else:
        # Tareas asignadas directamente a la carpeta (no a proyectos)
        tasks = q(
            f"SELECT t.id, t.title, t.location, t.notes, {effective_due_expr} AS due_date, t.due_time, t.completed_at, t.recurrence_rule, t.priority "
            "FROM tasks t "
            f"{recurring_due_join}"
            "WHERE t.folder_id=%s AND t.project_id IS NULL AND t.archived=0 AND t.deleted_at IS NULL AND t.completed_at IS NULL "
            f"ORDER BY ({effective_due_expr} IS NULL) ASC, {effective_due_expr} ASC, t.id DESC",
            (folder_id,)
        )

    tags_map = load_tags_map([t["id"] for t in tasks])
    task_ids = [t["id"] for t in tasks]

    sub_map = {}
    sub_counts = {}

    if task_ids:
        # Subtareas
        subs = q(
            "SELECT id, task_id, title, description, due_date, completed_at "
            "FROM subtasks "
            "WHERE task_id IN (" + ",".join(["%s"] * len(task_ids)) + ") "
            "ORDER BY (completed_at IS NOT NULL) ASC, (due_date IS NULL) ASC, due_date ASC, id ASC",
            tuple(task_ids),
        )
        for s in subs:
            sub_map.setdefault(s["task_id"], []).append(s)

        # Contadores
        cnt_rows = q(
            "SELECT task_id, COUNT(*) AS total, "
            "SUM(CASE WHEN completed_at IS NOT NULL THEN 1 ELSE 0 END) AS done "
            "FROM subtasks "
            "WHERE task_id IN (" + ",".join(["%s"] * len(task_ids)) + ") "
            "GROUP BY task_id",
            tuple(task_ids),
        )
        for r in cnt_rows:
            sub_counts[int(r["task_id"])] = {"total": int(r["total"]), "done": int(r["done"] or 0)}

    # Tareas completadas de la carpeta (normales + periódicas)
    completed_tasks = q(
        "SELECT * FROM ("
        "SELECT t.id, t.title, t.location, t.notes, t.due_date, t.completed_at, t.recurrence_rule, t.priority "
        "FROM tasks t "
        "WHERE t.folder_id=%s AND t.project_id IS NULL AND t.deleted_at IS NULL "
        "AND t.completed_at IS NOT NULL AND t.archived=0 "
        "UNION ALL "
        "SELECT t.id, t.title, t.location, t.notes, t.due_date, t.last_completed_at, t.recurrence_rule, t.priority "
        "FROM tasks t "
        "WHERE t.folder_id=%s AND t.project_id IS NULL AND t.deleted_at IS NULL "
        "AND t.last_completed_at IS NOT NULL "
        "AND t.recurrence_rule IS NOT NULL AND TRIM(t.recurrence_rule) <> '' "
        "AND t.archived=0 "
        ") AS completed_union "
        "ORDER BY completed_at DESC, id DESC "
        "LIMIT 10",
        (folder_id, folder_id)
    )
    completed_tags_map = load_tags_map([t["id"] for t in completed_tasks])

    return render_template(
        "folder_detail.html",
        folder=folder,
        folder_breadcrumb=folder_breadcrumb,
        projects=projects,
        tasks=tasks,
        tags_map=tags_map,
        sub_map=sub_map,
        sub_counts=sub_counts,
        completed_tasks=completed_tasks,
        completed_tags_map=completed_tags_map,
        include_subfolders=include_subfolders,
        tasks_grouped=tasks_grouped,
    )

@app.route("/folders/<int:folder_id>/rename", methods=["POST"])
def folder_rename(folder_id: int):
    new_name = normalize_name(request.form.get("name", ""))
    if not new_name:
        flash("Nombre de carpeta inválido.", "error")
        return redirect(request.referrer or url_for("folders_view"))

    try:

        exec_sql("UPDATE folders SET name=%s WHERE id=%s", (new_name, folder_id))
        commit()
        flash("Carpeta renombrada.", "ok")
    except Exception as e:
        rollback()
        flash(f"No se pudo renombrar: {e}", "error")

    return redirect(request.referrer or url_for("folders_view"))



@app.route("/folders/<int:folder_id>/move", methods=["GET", "POST"])
def folder_move(folder_id: int):
    folder = q1("SELECT id, name, parent_id FROM folders WHERE id=%s", (folder_id,))
    if not folder:
        abort(404)

    # Opciones de destino: todas menos la propia carpeta (y opcionalmente sus descendientes)
    # Para evitar ciclos perfectos, mínimo excluimos la propia.
    candidates = q(
        "SELECT id, name, parent_id FROM folders WHERE id<>%s ORDER BY name",
        (folder_id,),
    )

    if request.method == "POST":
        parent_raw = (request.form.get("parent_id") or "").strip()
        parent_id = None
        if parent_raw:
            try:
                parent_id = int(parent_raw)
            except ValueError:
                parent_id = None

        # Evitar hacerse hijo de sí misma
        if parent_id == folder_id:
            flash("No puedes seleccionar la misma carpeta como padre.", "error")
            return redirect(url_for("folder_move", folder_id=folder_id))

        try:
            exec_sql("UPDATE folders SET parent_id=%s WHERE id=%s", (parent_id, folder_id))
            commit()
            flash("Carpeta movida.", "ok")
            return redirect(url_for("folders"))
        except Exception as e:
            rollback()
            flash(f"No se pudo mover: {e}", "error")
            return redirect(url_for("folder_move", folder_id=folder_id))

    return render_template("folder_move.html", folder=folder, candidates=candidates)
    

@app.route("/folders/<int:folder_id>/delete", methods=["POST"])
def folder_delete(folder_id: int):
    # Siempre inicializadas (evita UnboundLocalError)
    children = []
    projs = []
    tasks_in_folder = {"c": 0}

    folder = q1("SELECT id, name, parent_id FROM folders WHERE id=%s", (folder_id,))
    if not folder:
        abort(404)

    # Hijas
    children = q("SELECT id, name FROM folders WHERE parent_id=%s ORDER BY name", (folder_id,))

    # Proyectos dentro (si tienes pf_folder_id o similar, ajusta el campo)
    # Si tu columna real es projects.folder_id o projects.folder_path_id, cambia esta consulta.
    projs = q("SELECT id, name FROM projects WHERE folder_id=%s AND archived=0 ORDER BY name", (folder_id,))

    # Tareas asignadas directamente a la carpeta
    tasks_in_folder = q1("SELECT COUNT(*) AS c FROM tasks WHERE folder_id=%s", (folder_id,)) or {"c": 0}

    if children or projs or int(tasks_in_folder["c"]) > 0:
        flash(
            "No se puede borrar la carpeta porque contiene subcarpetas, proyectos o tareas. "
            "Muévelos primero.",
            "error",
        )
        return redirect(url_for("folders"))  # o folder_detail si lo prefieres

    try:
        exec_sql("DELETE FROM folders WHERE id=%s", (folder_id,))
        commit()
        flash("Carpeta borrada.", "ok")
    except Exception as e:
        rollback()
        flash(f"No se pudo borrar la carpeta: {e}", "error")

    return redirect(url_for("folders"))


@app.route("/folders/<int:folder_id>/convert_to_project", methods=["POST"])
def folder_convert_to_project(folder_id: int):
    """Convierte la carpeta en un proyecto con el mismo nombre, en la carpeta padre.

    Sus tareas pasan al nuevo proyecto. Solo se permite si la carpeta no contiene
    subcarpetas ni proyectos (un proyecto no puede contenerlos).
    """
    folder = q1("SELECT id, name, parent_id FROM folders WHERE id=%s", (folder_id,))
    if not folder:
        abort(404)

    has_children = q1("SELECT 1 AS ok FROM folders WHERE parent_id=%s LIMIT 1", (folder_id,))
    has_projects = q1("SELECT 1 AS ok FROM projects WHERE folder_id=%s LIMIT 1", (folder_id,))
    if has_children or has_projects:
        flash(
            "No se puede convertir en proyecto: la carpeta contiene subcarpetas o proyectos "
            "(también archivados o en la papelera). Muévelos primero.",
            "error",
        )
        return redirect(url_for("folder_detail", folder_id=folder_id))

    try:
        project_id = exec_sql(
            "INSERT INTO projects(name, folder_id) VALUES(%s,%s)",
            (folder["name"], folder["parent_id"]),
        )
        exec_sql(
            "UPDATE tasks SET project_id=%s, folder_id=NULL WHERE folder_id=%s",
            (project_id, folder_id),
        )
        exec_sql("DELETE FROM folders WHERE id=%s", (folder_id,))
        commit()
    except Exception as e:
        rollback()
        flash(f"No se pudo convertir la carpeta en proyecto: {e}", "error")
        return redirect(url_for("folder_detail", folder_id=folder_id))

    flash(f"Carpeta '{folder['name']}' convertida en proyecto.", "ok")
    return redirect(url_for("project_detail", project_id=project_id))


@app.route("/folders/<int:folder_id>/purge_tasks", methods=["POST"])
def folder_purge_tasks(folder_id: int):
    folder = q1("SELECT id, name FROM folders WHERE id=%s", (folder_id,))
    if not folder:
        abort(404)

    try:
        total_row = q1(
            "SELECT COUNT(*) AS c FROM tasks WHERE folder_id=%s AND project_id IS NULL",
            (folder_id,),
        )
        total_to_delete = int(total_row["c"]) if total_row else 0

        if total_to_delete > 0:
            exec_sql(
                "DELETE tt FROM task_tags tt "
                "JOIN tasks t ON t.id=tt.task_id "
                "WHERE t.folder_id=%s AND t.project_id IS NULL",
                (folder_id,),
            )
            exec_sql(
                "DELETE st FROM subtasks st "
                "JOIN tasks t ON t.id=st.task_id "
                "WHERE t.folder_id=%s AND t.project_id IS NULL",
                (folder_id,),
            )
            exec_sql(
                "DELETE FROM tasks WHERE folder_id=%s AND project_id IS NULL",
                (folder_id,),
            )

        commit()
        flash(f"{total_to_delete} tareas de carpeta borradas.", "ok")
    except Exception as e:
        rollback()
        flash(f"No se pudieron vaciar las tareas de la carpeta: {e}", "error")

    return redirect(url_for("folder_detail", folder_id=folder_id))


@app.route("/folders/<int:folder_id>/archive_completed_tasks", methods=["POST"])
def folder_archive_completed_tasks(folder_id: int):
    folder = q1("SELECT id, name FROM folders WHERE id=%s", (folder_id,))
    if not folder:
        abort(404)
    periodic_names = tuple(role_tag_sql_names("periodic"))
    periodic_ph = ",".join(["LOWER(%s)"] * len(periodic_names))

    try:
        total_row = q1(
            "SELECT COUNT(*) AS c "
            "FROM tasks t "
            "WHERE t.folder_id=%s "
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
            (folder_id, *periodic_names),
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
                "    WHERE t.folder_id=%s "
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
                (folder_id, *periodic_names),
            )

        commit()
        flash(f"{total_to_archive} tareas realizadas archivadas.", "ok")
    except Exception as e:
        rollback()
        flash(f"No se pudieron archivar las tareas realizadas: {e}", "error")

    return redirect(url_for("folder_detail", folder_id=folder_id))


@app.route("/folders/<int:folder_id>/purge_all", methods=["POST"])
def folder_purge_all(folder_id: int):
    folder = q1("SELECT id, name FROM folders WHERE id=%s", (folder_id,))
    if not folder:
        abort(404)

    try:
        projects_row = q1(
            "SELECT COUNT(*) AS c FROM projects WHERE folder_id=%s",
            (folder_id,),
        )
        tasks_row = q1(
            "SELECT COUNT(*) AS c "
            "FROM tasks t "
            "WHERE (t.folder_id=%s AND t.project_id IS NULL) "
            "OR t.project_id IN (SELECT p.id FROM projects p WHERE p.folder_id=%s)",
            (folder_id, folder_id),
        )
        total_projects = int(projects_row["c"]) if projects_row else 0
        total_tasks = int(tasks_row["c"]) if tasks_row else 0

        if total_tasks > 0:
            exec_sql(
                "DELETE tt FROM task_tags tt "
                "JOIN tasks t ON t.id=tt.task_id "
                "WHERE (t.folder_id=%s AND t.project_id IS NULL) "
                "OR t.project_id IN (SELECT p.id FROM projects p WHERE p.folder_id=%s)",
                (folder_id, folder_id),
            )
            exec_sql(
                "DELETE st FROM subtasks st "
                "JOIN tasks t ON t.id=st.task_id "
                "WHERE (t.folder_id=%s AND t.project_id IS NULL) "
                "OR t.project_id IN (SELECT p.id FROM projects p WHERE p.folder_id=%s)",
                (folder_id, folder_id),
            )
            exec_sql(
                "DELETE FROM tasks "
                "WHERE (folder_id=%s AND project_id IS NULL) "
                "OR project_id IN (SELECT p.id FROM projects p WHERE p.folder_id=%s)",
                (folder_id, folder_id),
            )

        if total_projects > 0:
            exec_sql("DELETE FROM projects WHERE folder_id=%s", (folder_id,))

        commit()
        flash(
            f"Carpeta vaciada: {total_tasks} tareas y {total_projects} proyectos borrados.",
            "ok",
        )
    except Exception as e:
        rollback()
        flash(f"No se pudo vaciar la carpeta: {e}", "error")

    return redirect(url_for("folder_detail", folder_id=folder_id))


@app.route("/api/folders/<int:folder_id>/move", methods=["POST"])
def api_folder_move(folder_id: int):
    payload = request.get_json(silent=True) or {}
    parent_id_raw = payload.get("parent_id")

    folder = q1("SELECT id FROM folders WHERE id=%s", (folder_id,))
    if not folder:
        return jsonify({"ok": False, "error": "Carpeta no encontrada"}), 404

    parent_id = None
    if parent_id_raw is not None and str(parent_id_raw).strip() != "":
        try:
            parent_id = int(parent_id_raw)
        except Exception:
            return jsonify({"ok": False, "error": "Carpeta destino inválida"}), 400

    if parent_id == folder_id:
        return jsonify({"ok": False, "error": "No puedes mover una carpeta dentro de sí misma"}), 400

    if parent_id is not None:
        parent = q1("SELECT id FROM folders WHERE id=%s", (parent_id,))
        if not parent:
            return jsonify({"ok": False, "error": "Carpeta destino no válida"}), 400

        # Evita ciclos: el nuevo padre no puede ser un descendiente de la carpeta movida.
        rows = q("SELECT id, parent_id FROM folders")
        by_id: Dict[int, Optional[int]] = {}
        for r in rows:
            by_id[int(r["id"])] = int(r["parent_id"]) if r.get("parent_id") is not None else None

        cursor = parent_id
        visited: set[int] = set()
        while cursor is not None and cursor not in visited:
            if cursor == folder_id:
                return jsonify({"ok": False, "error": "No se puede mover una carpeta dentro de una subcarpeta suya"}), 400
            visited.add(cursor)
            cursor = by_id.get(cursor)

    try:
        exec_sql("UPDATE folders SET parent_id=%s WHERE id=%s", (parent_id, folder_id))
        commit()
        return jsonify({"ok": True, "message": "Carpeta movida"})
    except Exception as e:
        rollback()
        return jsonify({"ok": False, "error": str(e)}), 500
