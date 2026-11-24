"""Pantallas de conflictos y estado de la sincronización con Google Calendar."""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Dict, List, Optional

from flask import flash, redirect, render_template, request, session, url_for

from gtd.config import cfg_int
from gtd.utils import _today_madrid, safe_next_url
from gtd.core import app
from gtd.db import commit, exec_sql, q, q1, rollback
from gtd.calendar_sync import (
    _apply_google_to_task,
    _calendar_sync_lock_acquire,
    _calendar_sync_lock_release,
    _calendar_sync_service,
    _create_task_from_calendar_event,
    _google_event_to_task_fields,
    _is_retryable_db_error,
    _sync_task_push,
    calendar_sync_calendar_id,
    ensure_calendar_pending_events_table,
    run_calendar_pull_sync,
    run_calendar_push_sync,
)
from gtd.common import admin_required


def _load_calendar_conflicts_view_data(page: int) -> Dict[str, Any]:
    per_page = cfg_int(["app", "pagination", "calendar_conflicts_per_page"], default=25, min_v=5, max_v=500)

    total_row = q1("SELECT COUNT(*) AS c FROM tasks WHERE calendar_sync_state='conflict'")
    total = int(total_row["c"]) if total_row else 0

    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, pages))
    offset = (page - 1) * per_page

    def _norm_hhmm(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, timedelta):
            total_seconds = int(value.total_seconds()) % (24 * 3600)
            hh = total_seconds // 3600
            mm = (total_seconds % 3600) // 60
            return f"{hh:02d}:{mm:02d}"
        if hasattr(value, "strftime"):
            try:
                return value.strftime("%H:%M")
            except Exception:
                pass
        txt = str(value).strip()
        if len(txt) >= 5 and txt[2] == ":":
            return txt[:5]
        return txt

    def _calendar_conflict_reason(row: Dict[str, Any]) -> str:
        payload_raw = row.get("calendar_conflict_payload")
        if not payload_raw:
            return "No disponible"

        try:
            ev = json.loads(payload_raw)
            remote = _google_event_to_task_fields(ev)
        except Exception:
            return "No disponible"

        reasons: List[str] = []

        if (row.get("title") or "").strip() != (remote.get("title") or "").strip():
            reasons.append("título")

        if (row.get("notes") or "").strip() != (remote.get("notes") or "").strip():
            reasons.append("notas")

        if str(row.get("due_date") or "") != str(remote.get("due_date") or ""):
            reasons.append("fecha")

        if _norm_hhmm(row.get("due_time")) != _norm_hhmm(remote.get("due_time")):
            reasons.append("hora")

        if not reasons:
            return "Sin diferencias actuales (conflicto pendiente heredado)"
        return ", ".join(reasons)

    calendar_conflicts = q(
        "SELECT id, title, notes, due_date, due_time, TIME_FORMAT(due_time, '%%H:%%i') AS due_time_text, "
        "calendar_conflict_at, calendar_conflict_payload "
        "FROM tasks "
        "WHERE calendar_sync_state='conflict' "
        "ORDER BY calendar_conflict_at DESC, id DESC "
        "LIMIT %s OFFSET %s",
        (per_page, offset),
    )

    for item in calendar_conflicts:
        item["conflict_reason"] = _calendar_conflict_reason(item)

    calendar_sync_stats = {
        "pending_push": 0,
        "pending_delete": 0,
        "conflict": 0,
        "error": 0,
    }

    stats_rows = q(
        "SELECT calendar_sync_state, COUNT(*) AS c "
        "FROM tasks "
        "WHERE calendar_sync_state IN ('pending_push','pending_delete','conflict','error') "
        "GROUP BY calendar_sync_state"
    )
    for r in stats_rows:
        st = r.get("calendar_sync_state")
        if st in calendar_sync_stats:
            calendar_sync_stats[st] = int(r.get("c") or 0)

    return {
        "calendar_conflicts": calendar_conflicts,
        "calendar_sync_stats": calendar_sync_stats,
        "page": page,
        "pages": pages,
        "total": total,
        "per_page": per_page,
    }


def _load_calendar_sync_view_data(
    page: int,
    only_conflicts: bool = False,
    hide_overdue: bool = False,
    show_hidden: bool = False,
) -> Dict[str, Any]:
    per_page = cfg_int(["app", "pagination", "calendar_conflicts_per_page"], default=25, min_v=5, max_v=500)
    today_d = _today_madrid()

    where_clauses = [
        "(calendar_sync_state IN ('pending_push','pending_delete','conflict','error') OR (calendar_sync_ignored=1 AND %s=1))",
        "(due_date IS NOT NULL OR google_event_id IS NOT NULL OR calendar_sync_ignored=1)",
    ]
    where_params: List[Any] = [1 if show_hidden else 0]
    if only_conflicts:
        where_clauses[0] = "calendar_sync_state='conflict'"
        where_params = []
    if hide_overdue:
        where_clauses.append("(due_date IS NULL OR due_date >= %s)")
        where_params.append(today_d)
    base_where = " AND ".join(where_clauses)

    total_row = q1(f"SELECT COUNT(*) AS c FROM tasks WHERE {base_where}", tuple(where_params))
    total = int(total_row["c"]) if total_row else 0

    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, pages))
    offset = (page - 1) * per_page

    def _norm_hhmm(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, timedelta):
            total_seconds = int(value.total_seconds()) % (24 * 3600)
            hh = total_seconds // 3600
            mm = (total_seconds % 3600) // 60
            return f"{hh:02d}:{mm:02d}"
        if hasattr(value, "strftime"):
            try:
                return value.strftime("%H:%M")
            except Exception:
                pass
        txt = str(value).strip()
        if len(txt) >= 5 and txt[2] == ":":
            return txt[:5]
        return txt

    def _calendar_conflict_reason(row: Dict[str, Any]) -> str:
        payload_raw = row.get("calendar_conflict_payload")
        if not payload_raw:
            return "No disponible"

        try:
            ev = json.loads(payload_raw)
            remote = _google_event_to_task_fields(ev)
        except Exception:
            return "No disponible"

        reasons: List[str] = []

        if (row.get("title") or "").strip() != (remote.get("title") or "").strip():
            reasons.append("titulo")

        if (row.get("notes") or "").strip() != (remote.get("notes") or "").strip():
            reasons.append("notas")

        if str(row.get("due_date") or "") != str(remote.get("due_date") or ""):
            reasons.append("fecha")

        if _norm_hhmm(row.get("due_time")) != _norm_hhmm(remote.get("due_time")):
            reasons.append("hora")

        if not reasons:
            return "Sin diferencias actuales"
        return ", ".join(reasons)

    rows = q(
        f"SELECT id, title, notes, due_date, due_time, TIME_FORMAT(due_time, '%%H:%%i') AS due_time_text, "
        "google_event_id, "
        "calendar_sync_state, calendar_sync_ignored, calendar_sync_error, calendar_conflict_at, calendar_conflict_payload "
        "FROM tasks "
        f"WHERE {base_where} "
        "ORDER BY "
        "CASE calendar_sync_state "
        "  WHEN 'conflict' THEN 1 "
        "  WHEN 'pending_push' THEN 2 "
        "  WHEN 'pending_delete' THEN 3 "
        "  WHEN 'error' THEN 4 "
        "  ELSE 5 END, "
        "COALESCE(calendar_conflict_at, NOW()) DESC, id DESC "
        "LIMIT %s OFFSET %s",
        tuple(where_params + [per_page, offset]),
    )

    calendar_sync_items: List[Dict[str, Any]] = []
    for r in rows:
        state = (r.get("calendar_sync_state") or "").strip().lower()
        ignored = int(r.get("calendar_sync_ignored") or 0) == 1
        if ignored:
            origin = "GTD"
            state_label = "Oculto · Solo GTD"
            action_defs = [{"key": "ask_again", "label": "Volver a preguntar"}]
            actionable = True
            reason = "Se conserva solo en GTD; no se volverá a proponer su envío"
            default_action_key = "ask_again"
        elif state == "conflict":
            origin = "Ambos"
            state_label = "Con diferencias"
            action_defs = [
                {"key": "keep_gtd", "label": "Usar GTD"},
                {"key": "keep_google", "label": "Usar GCal"},
            ]
            actionable = True
            reason = _calendar_conflict_reason(r)
            default_action_key = "keep_gtd"
        elif state == "pending_push":
            origin = "GTD"
            state_label = "Solo en GTD"
            action_defs = [
                {"key": "create_gcal", "label": "Crear en GCal"},
                {"key": "solo_gtd", "label": "Solo GTD"},
                {"key": "no_ask", "label": "No volver a preguntar"},
            ]
            actionable = False
            reason = "Pendiente de envio a Calendar"
            default_action_key = "create_gcal"
        elif state == "pending_delete":
            origin = "Ambos"
            state_label = "Pendiente borrado en GCal"
            action_defs = [
                {"key": "delete_gcal", "label": "Borrar en GCal"},
                {"key": "solo_gtd", "label": "Solo GTD"},
            ]
            actionable = False
            reason = "Existe en GCal; pendiente de borrado remoto"
            default_action_key = "delete_gcal"
        else:
            origin = "Ambos"
            state_label = "Error"
            action_defs = [
                {"key": "retry_sync", "label": "Reintentar"},
                {"key": "solo_gtd", "label": "Solo GTD"},
            ]
            actionable = False
            reason = "Error de sincronización con Google Calendar"
            default_action_key = "retry_sync"

        item = dict(r)
        item["origin"] = origin
        item["state_label"] = state_label
        item["action_defs"] = action_defs
        item["actionable"] = actionable
        item["reason"] = reason
        item["default_action_key"] = default_action_key
        calendar_sync_items.append(item)

    calendar_sync_stats = {
        "pending_push": 0,
        "pending_delete": 0,
        "conflict": 0,
        "error": 0,
    }

    stats_rows = q(
        "SELECT calendar_sync_state, COUNT(*) AS c "
        "FROM tasks "
        "WHERE calendar_sync_state IN ('pending_push','pending_delete','conflict','error') "
        "GROUP BY calendar_sync_state"
    )
    for r in stats_rows:
        st = r.get("calendar_sync_state")
        if st in calendar_sync_stats:
            calendar_sync_stats[st] = int(r.get("c") or 0)

    # Cargar eventos de GCal pendientes de revisión (staging)
    pending_gcal_items: List[Dict[str, Any]] = []
    pending_gcal_total = 0
    try:
        ensure_calendar_pending_events_table()
        pending_where = "state IN ('pending','ignored') AND due_date IS NOT NULL"
        if not show_hidden:
            pending_where += " AND state='pending'"
        pending_params: List[Any] = []
        if hide_overdue:
            pending_where += " AND due_date >= %s"
            pending_params.append(today_d)

        pending_count_row = q1(
            f"SELECT COUNT(*) AS c FROM calendar_pending_events WHERE {pending_where}",
            tuple(pending_params),
        )
        pending_gcal_total = int((pending_count_row or {}).get("c") or 0)
        pending_rows = q(
            "SELECT id, google_event_id, title, due_date, due_time, state, "
            "TIME_FORMAT(due_time, '%%H:%%i') AS due_time_text, notes, discovered_at "
            "FROM calendar_pending_events "
            f"WHERE {pending_where} "
            "ORDER BY discovered_at DESC "
            "LIMIT 200",
            tuple(pending_params),
        )
        for pr in pending_rows:
            pitem: Dict[str, Any] = {
                "id": f"p_{pr['id']}",
                "title": pr.get("title") or "(sin título)",
                "notes": pr.get("notes") or "",
                "due_date": pr.get("due_date"),
                "due_time": pr.get("due_time"),
                "due_time_text": pr.get("due_time_text") or "—",
                "calendar_sync_state": "gcal_pending",
                "calendar_sync_error": None,
                "calendar_conflict_at": None,
                "calendar_conflict_payload": None,
                "origin": "GCalendar",
                "state_label": "Oculto · Solo GCal" if pr.get("state") == "ignored" else "Sin importar",
                "action_defs": ([{"key": "ask_again", "label": "Volver a preguntar"}] if pr.get("state") == "ignored" else [
                    {"key": "import_gtd", "label": "Crear en GTD"},
                    {"key": "solo_gcal", "label": "Solo GCal"},
                    {"key": "no_ask", "label": "No volver a preguntar"},
                ]),
                "actionable": True,
                "reason": "Evento de Google Calendar sin tarea GTD asociada",
                "default_action_key": "ask_again" if pr.get("state") == "ignored" else "import_gtd",
            }
            pending_gcal_items.append(pitem)
    except Exception:
        pass

    return {
        "calendar_sync_items": calendar_sync_items,
        "calendar_sync_stats": calendar_sync_stats,
        "pending_gcal_items": pending_gcal_items,
        "pending_gcal_total": pending_gcal_total,
        "page": page,
        "pages": pages,
        "total": total,
        "per_page": per_page,
        "only_conflicts": only_conflicts,
        "hide_overdue": hide_overdue,
        "show_hidden": show_hidden,
    }


def _handle_calendar_conflicts_action(action: str, default_endpoint: str):
    handled = {
        "sync_calendar_now",
        "apply_calendar_sync_changes",
        "resolve_calendar_conflict",
        "resolve_calendar_conflicts_all",
        "resolve_calendar_conflicts_selected",
    }
    if action not in handled:
        return None

    next_url = safe_next_url(request.form.get("next"), default_endpoint)

    if not admin_required():
        flash("No autorizado.", "error")
        return redirect(next_url)

    if action == "sync_calendar_now":
        acquired = False
        import_mode = (request.form.get("import_mode") or "event_date").strip().lower()
        if import_mode not in ("event_date", "created_date"):
            import_mode = "event_date"
        range_value = (request.form.get("range_value") or "15days").strip().lower()
        if range_value not in ("today", "7days", "15days"):
            range_value = "15days"
        try:
            acquired = _calendar_sync_lock_acquire(timeout_seconds=2)
            if not acquired:
                session["calendar_sync_last_info"] = "Ya hay una sincronización Calendar en curso. Inténtalo de nuevo en unos segundos."
                session["calendar_sync_last_level"] = "error"
                return redirect(next_url)

            last_exc: Optional[Exception] = None
            pull_res: Dict[str, Any] = {}
            push_res: Dict[str, Any] = {}

            for attempt in range(2):
                try:
                    service = _calendar_sync_service()
                    if service is None:
                        session["calendar_sync_last_info"] = "No hay credenciales de Google Calendar disponibles."
                        session["calendar_sync_last_level"] = "error"
                        return redirect(next_url)

                    pull_res = run_calendar_pull_sync(
                        force=True,
                        service=service,
                        max_pages=4,
                        time_budget_seconds=12,
                        discover_mode=import_mode,
                        discover_range=range_value,
                        stage_only=True,
                    )
                    push_res = run_calendar_push_sync(limit=500, service=service)
                    commit()
                    last_exc = None
                    break
                except Exception as e:
                    rollback()
                    last_exc = e
                    if _is_retryable_db_error(e) and attempt == 0:
                        continue
                    raise

            if last_exc is not None:
                raise last_exc

            partial = " (parcial por límite de tiempo/páginas)" if pull_res.get("truncated") else ""
            session["calendar_sync_last_info"] = (
                "Sync Calendar: "
                f"en GTD (updated={pull_res['updated']}, conflictos={pull_res['conflicts']}, archivados={pull_res['archived']}, pendientes_revisar={pull_res.get('imported', 0)}) "
                f"en GCalendar (ok={push_res['ok']}, fail={push_res['fail']}).{partial}"
            )
            session["calendar_sync_last_level"] = "ok"
        except Exception as e:
            rollback()
            session["calendar_sync_last_info"] = f"No se pudo sincronizar con Google Calendar: {e}"
            session["calendar_sync_last_level"] = "error"
        finally:
            if acquired:
                _calendar_sync_lock_release()
        return redirect(next_url)

    if action == "apply_calendar_sync_changes":
        raw_ids = request.form.getlist("task_ids")
        task_ids: List[int] = []
        pending_pids: List[int] = []
        for rid in raw_ids:
            rid = rid.strip()
            if rid.startswith("p_"):
                try:
                    pending_pids.append(int(rid[2:]))
                except Exception:
                    pass
            else:
                try:
                    task_ids.append(int(rid))
                except Exception:
                    pass
        task_ids = sorted(set(task_ids))[:500]
        pending_pids = sorted(set(pending_pids))[:200]

        if not task_ids and not pending_pids:
            flash("No hay elementos seleccionados para aplicar cambios.", "error")
            return redirect(next_url)

        rows = []
        if task_ids:
            placeholders = ",".join(["%s"] * len(task_ids))
            rows = q(
                "SELECT id, calendar_sync_state, calendar_sync_ignored, calendar_conflict_payload "
                "FROM tasks "
                f"WHERE id IN ({placeholders})",
                tuple(task_ids),
            )

        state_by_id: Dict[int, str] = {int(r["id"]): (r.get("calendar_sync_state") or "") for r in rows}
        ignored_by_id: Dict[int, bool] = {int(r["id"]): bool(int(r.get("calendar_sync_ignored") or 0)) for r in rows}
        payload_by_id: Dict[int, Any] = {int(r["id"]): r.get("calendar_conflict_payload") for r in rows}

        applied = 0
        skipped = 0
        failed = 0
        first_error: Optional[str] = None

        try:
            service = _calendar_sync_service()

            for task_id in task_ids:
                st = (state_by_id.get(task_id) or "").strip().lower()
                chosen = (request.form.get(f"row_action_{task_id}") or "").strip().lower()

                if not chosen:
                    skipped += 1
                    continue

                if chosen == "ask_again" and ignored_by_id.get(task_id, False):
                    try:
                        exec_sql(
                            "UPDATE tasks SET calendar_sync_ignored=0, calendar_sync_state='pending_push' WHERE id=%s",
                            (task_id,),
                        )
                        applied += 1
                    except Exception as e:
                        failed += 1
                        if first_error is None:
                            first_error = str(e)
                    continue

                try:
                    if st == "conflict":
                        if chosen == "keep_google":
                            payload = payload_by_id.get(task_id)
                            if not payload:
                                skipped += 1
                                continue
                            ev = json.loads(payload)
                            _apply_google_to_task(task_id, ev)
                            exec_sql(
                                "UPDATE tasks "
                                "SET calendar_sync_state='synced', calendar_conflict_payload=NULL, calendar_conflict_at=NULL "
                                "WHERE id=%s",
                                (task_id,),
                            )
                            applied += 1
                            continue

                        if chosen == "keep_gtd":
                            exec_sql(
                                "UPDATE tasks "
                                "SET calendar_sync_state='pending_push', calendar_conflict_payload=NULL, calendar_conflict_at=NULL, calendar_local_changed_at=NOW() "
                                "WHERE id=%s",
                                (task_id,),
                            )
                            if _sync_task_push(task_id, service=service, force_remote_update=True):
                                applied += 1
                            else:
                                failed += 1
                            continue

                        skipped += 1
                        continue

                    if st in ("pending_push", "pending_delete", "error"):
                        if chosen == "no_ask" and st == "pending_push":
                            exec_sql(
                                "UPDATE tasks SET calendar_sync_ignored=1, calendar_sync_state='none', "
                                "calendar_sync_error=NULL, calendar_local_changed_at=NULL WHERE id=%s",
                                (task_id,),
                            )
                            applied += 1
                            continue

                        if chosen in ("create_gcal", "delete_gcal", "retry_sync"):
                            if _sync_task_push(task_id, service=service, force_remote_update=True):
                                applied += 1
                            else:
                                failed += 1
                                if first_error is None:
                                    row_err = q1("SELECT calendar_sync_error FROM tasks WHERE id=%s", (task_id,))
                                    first_error = (row_err or {}).get("calendar_sync_error") or "Error al sincronizar con Google Calendar"
                            continue

                        if chosen == "solo_gtd":
                            exec_sql(
                                "UPDATE tasks "
                                "SET calendar_sync_state='none', calendar_sync_error=NULL, calendar_local_changed_at=NULL "
                                "WHERE id=%s",
                                (task_id,),
                            )
                            applied += 1
                            continue

                        skipped += 1
                        continue

                    skipped += 1
                except Exception as e:
                    failed += 1
                    if first_error is None:
                        first_error = str(e)

            # Procesar eventos de GCal pendientes de revisión (staging)
            for pid in pending_pids:
                chosen = (request.form.get(f"row_action_p_{pid}") or "").strip().lower()
                if not chosen:
                    skipped += 1
                    continue
                try:
                    pending_row = q1(
                        "SELECT id, google_event_id, google_calendar_id, event_data "
                        "FROM calendar_pending_events WHERE id=%s AND state IN ('pending','ignored')",
                        (pid,),
                    )
                    if not pending_row:
                        skipped += 1
                        continue
                    if chosen == "import_gtd":
                        ev_data = json.loads(pending_row.get("event_data") or "{}")
                        cal_id = (pending_row.get("google_calendar_id") or calendar_sync_calendar_id())
                        new_task_id = _create_task_from_calendar_event(ev_data, cal_id)
                        if new_task_id:
                            exec_sql(
                                "UPDATE calendar_pending_events SET state='imported', task_id=%s WHERE id=%s",
                                (new_task_id, pid),
                            )
                            applied += 1
                        else:
                            skipped += 1
                    elif chosen in ("solo_gcal", "no_ask"):
                        exec_sql(
                            "UPDATE calendar_pending_events SET state='ignored' WHERE id=%s",
                            (pid,),
                        )
                        applied += 1
                    elif chosen == "ask_again":
                        exec_sql(
                            "UPDATE calendar_pending_events SET state='pending' WHERE id=%s AND state='ignored'",
                            (pid,),
                        )
                        applied += 1
                    else:
                        skipped += 1
                except Exception as e:
                    failed += 1
                    if first_error is None:
                        first_error = str(e)

            commit()
            detail = ""
            if first_error:
                detail_low = first_error.lower()
                if (
                    "insufficient authentication scopes" in detail_low
                    or "insufficientpermissions" in detail_low
                    or "insufficient authentica" in detail_low
                    or "forbidden" in detail_low
                ):
                    detail = (
                        " Reautoriza Google OAuth con permisos de Calendar (write): "
                        "regenera instance/gmail_token.json."
                    )
                else:
                    detail = f" Primer error: {first_error[:220]}"

            flash(
                f"Aplicar cambios: {applied} aplicados, {skipped} omitidos, {failed} con error.",
                "ok" if failed == 0 else "error",
            )
            if detail:
                flash(detail.strip(), "error_detail")
        except Exception as e:
            rollback()
            flash(f"No se pudieron aplicar cambios: {e}", "error")

        return redirect(next_url)

    if action == "resolve_calendar_conflict":
        task_id_raw = (request.form.get("task_id") or "").strip()
        resolution = (request.form.get("resolution") or "").strip()

        try:
            task_id = int(task_id_raw)
        except Exception:
            flash("task_id inválido.", "error")
            return redirect(next_url)

        task = q1(
            "SELECT id, calendar_conflict_payload FROM tasks WHERE id=%s",
            (task_id,),
        )
        if not task:
            flash("Tarea no encontrada.", "error")
            return redirect(next_url)

        try:
            success_flash = ""
            if resolution == "keep_google":
                payload = task.get("calendar_conflict_payload")
                if not payload:
                    flash("No hay payload de conflicto en la tarea.", "error")
                    return redirect(next_url)
                ev = json.loads(payload)

                # Preferimos leer el evento actual en Google para evitar aplicar un snapshot obsoleto.
                event_id = (ev.get("id") or "").strip()
                if event_id:
                    service = _calendar_sync_service()
                    if service is not None:
                        try:
                            ev_live = service.events().get(
                                calendarId=calendar_sync_calendar_id(),
                                eventId=event_id,
                            ).execute()
                            if (ev_live.get("status") or "").strip().lower() != "cancelled":
                                ev = ev_live
                        except Exception:
                            pass

                _apply_google_to_task(task_id, ev)
                exec_sql(
                    "UPDATE tasks "
                    "SET calendar_sync_state='synced', calendar_conflict_payload=NULL, calendar_conflict_at=NULL "
                    "WHERE id=%s",
                    (task_id,),
                )
                success_flash = "Actualizado en GTD."
            elif resolution == "keep_gtd":
                exec_sql(
                    "UPDATE tasks "
                    "SET calendar_sync_state='pending_push', calendar_conflict_payload=NULL, calendar_conflict_at=NULL, calendar_local_changed_at=NOW() "
                    "WHERE id=%s",
                    (task_id,),
                )
                service = _calendar_sync_service()
                if not _sync_task_push(task_id, service=service, force_remote_update=True):
                    raise RuntimeError("No se pudo aplicar GTD en Google Calendar")
                success_flash = "Actualizado en GCalendar."
            else:
                flash("Resolución de conflicto inválida.", "error")
                return redirect(next_url)

            commit()
            flash(success_flash or "Conflicto resuelto.", "ok")
        except Exception as e:
            rollback()
            flash(f"No se pudo resolver el conflicto: {e}", "error")

        return redirect(next_url)

    resolution = (request.form.get("resolution") or "").strip()
    if resolution not in ("keep_google", "keep_gtd"):
        flash("Resolución masiva inválida.", "error")
        return redirect(next_url)

    if action == "resolve_calendar_conflicts_selected":
        raw_ids = request.form.getlist("task_ids")
        task_ids: List[int] = []
        for rid in raw_ids:
            try:
                task_ids.append(int(rid))
            except Exception:
                pass
        task_ids = sorted(set(task_ids))[:200]

        if not task_ids:
            flash("No hay conflictos seleccionados.", "error")
            return redirect(next_url)

        placeholders = ",".join(["%s"] * len(task_ids))
        rows = q(
            "SELECT id, calendar_conflict_payload "
            "FROM tasks "
            f"WHERE calendar_sync_state='conflict' AND id IN ({placeholders}) "
            "ORDER BY calendar_conflict_at DESC, id DESC",
            tuple(task_ids),
        )
    else:
        rows = q(
            "SELECT id, calendar_conflict_payload "
            "FROM tasks "
            "WHERE calendar_sync_state='conflict' "
            "ORDER BY calendar_conflict_at DESC, id DESC "
            "LIMIT 200"
        )

    if not rows:
        flash("No hay conflictos para resolver.", "ok")
        return redirect(next_url)

    resolved = 0
    failed = 0

    try:
        service = _calendar_sync_service() if resolution == "keep_gtd" else None

        for row in rows:
            task_id = int(row["id"])
            try:
                if resolution == "keep_google":
                    payload = row.get("calendar_conflict_payload")
                    if not payload:
                        failed += 1
                        continue
                    ev = json.loads(payload)
                    _apply_google_to_task(task_id, ev)
                    exec_sql(
                        "UPDATE tasks "
                        "SET calendar_sync_state='synced', calendar_conflict_payload=NULL, calendar_conflict_at=NULL "
                        "WHERE id=%s",
                        (task_id,),
                    )
                else:
                    exec_sql(
                        "UPDATE tasks "
                        "SET calendar_sync_state='pending_push', calendar_conflict_payload=NULL, calendar_conflict_at=NULL, calendar_local_changed_at=NOW() "
                        "WHERE id=%s",
                        (task_id,),
                    )
                    if not _sync_task_push(task_id, service=service, force_remote_update=True):
                        failed += 1
                        continue

                resolved += 1
            except Exception:
                failed += 1

        commit()
        target_label = "Actualizado en GTD" if resolution == "keep_google" else "Actualizado en GCalendar"
        flash(
            f"Resolución masiva completada ({'Usar Google' if resolution == 'keep_google' else 'Usar GTD'} · {target_label}): "
            f"{resolved} resueltos, {failed} con error.",
            "ok" if failed == 0 else "error",
        )
    except Exception as e:
        rollback()
        flash(f"No se pudo resolver en bloque: {e}", "error")

    return redirect(next_url)


@app.route("/calendar/conflicts", methods=["GET", "POST"])
def calendar_conflicts_view():
    if request.method == "POST":
        return redirect(url_for("calendar_sync_view"), code=307)
    return redirect(url_for("calendar_sync_view"), code=301)


@app.route("/calendar/sync", methods=["GET", "POST"])
def calendar_sync_view():
    if not admin_required():
        flash("No autorizado.", "error")
        return redirect(url_for("admin"))

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()
        handled = _handle_calendar_conflicts_action(action, "calendar_sync_view")
        if handled is not None:
            return handled
        flash("Acción de sincronización no válida.", "error")
        return redirect(url_for("calendar_sync_view"))

    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1

    only_conflicts = str(request.args.get("only_conflicts", "0")).strip().lower() in ("1", "true", "on", "yes")
    hide_overdue = str(request.args.get("hide_overdue", "0")).strip().lower() in ("1", "true", "on", "yes")
    show_hidden = str(request.args.get("show_hidden", "0")).strip().lower() in ("1", "true", "on", "yes")

    context = _load_calendar_sync_view_data(
        page=page,
        only_conflicts=only_conflicts,
        hide_overdue=hide_overdue,
        show_hidden=show_hidden,
    )
    calendar_sync_last_info = session.pop("calendar_sync_last_info", None)
    calendar_sync_last_level = session.pop("calendar_sync_last_level", "ok")
    return render_template(
        "calendar_sync.html",
        calendar_sync_last_info=calendar_sync_last_info,
        calendar_sync_last_level=calendar_sync_last_level,
        **context,
    )
