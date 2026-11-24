"""Importación Gmail/Calendar al Inbox y sincronización manual."""
from __future__ import annotations

import re

from flask import flash, redirect, request, session

from calendar_import import (
    build_google_service,
    list_recent_events_by_created,
    list_upcoming_events,
)
from gmail_import import (
    build_gmail_service,
    get_message_metadata,
    list_matching_messages,
    message_to_task_payload,
)

from gtd.utils import normalize_name, safe_next_url
from gtd.core import app
from gtd.db import commit, exec_sql, rollback
from gtd.lookups import find_folder_by_name, find_project_by_name_active, get_or_create_tag
from gtd.roles import role_tag
from gtd.quick_entry import (
    _remove_unquoted_matches,
    _unquote_title,
    _unquoted_matches,
    extract_due_date_from_quick,
    extract_due_time_from_quick,
    extract_recurrence_from_quick,
    parse_task_quick_entry,
    PROJ_RE,
    QUICK_TAG_RE,
    TIME_TOKEN_RE,
)
from gtd.gmail import (
    _is_google_invalid_grant_error,
    ensure_imported_emails_table,
    gmail_credentials_path,
    gmail_default_query,
    gmail_message_already_imported,
    gmail_token_path,
)
from gtd.calendar_sync import (
    _calendar_sync_service,
    _google_event_to_task_fields,
    calendar_event_already_imported,
    calendar_sync_calendar_id,
    ensure_imported_calendar_events_table,
    google_credentials_path,
    google_token_path,
    run_calendar_pull_sync,
    run_calendar_push_sync,
)


@app.route("/gmail/import_to_inbox", methods=["POST"])
def gmail_import_to_inbox():
    """
    Importa correos de Gmail como tareas en Inbox.

    Requisitos:
    - instance/gmail_credentials.json
    - instance/gmail_token.json
    - tabla imported_emails
    """
    next_url = safe_next_url(request.form.get("next"), "home")

    try:
        ensure_imported_emails_table()

        creds_path = gmail_credentials_path()
        token_path = gmail_token_path()

        if not creds_path.exists():
            flash(
                "Falta instance/gmail_credentials.json. "
                "Descarga el OAuth client de Google Cloud y colócalo ahí.",
                "error",
            )
            return redirect(next_url)

        service = build_gmail_service(creds_path, token_path)

        gmail_query = (request.form.get("gmail_query") or "").strip() or gmail_default_query()
        max_results_raw = (request.form.get("max_results") or "").strip()

        try:
            max_results = int(max_results_raw) if max_results_raw else 20
        except ValueError:
            max_results = 20

        max_results = max(1, min(max_results, 100))

        found = list_matching_messages(service, gmail_query=gmail_query, max_results=max_results)

        if not found:
            flash("No se encontraron correos para importar.", "ok")
            return redirect(next_url)

        created = 0
        skipped = 0

        for item in found:
            message_id = item.get("id")
            if not message_id:
                skipped += 1
                continue

            if gmail_message_already_imported(message_id):
                skipped += 1
                continue

            full_msg = get_message_metadata(service, message_id)
            payload = message_to_task_payload(full_msg)

            raw = (payload.get("title") or "").strip()
            raw_work = raw

            # Parseo estilo task_create sobre el subject del correo.
            tags = [match.group(1) for match in _unquoted_matches(QUICK_TAG_RE, raw_work)]

            detected_due_date = None
            detected_due_time = None
            if not payload.get("due_date"):
                detected_due_date, raw_work = extract_due_date_from_quick(raw_work)
            detected_due_time, raw_work = extract_due_time_from_quick(raw_work)

            recurrence, raw_work = extract_recurrence_from_quick(raw_work)

            project_name = None
            project_candidates = _unquoted_matches(PROJ_RE, raw_work)
            for candidate in project_candidates:
                candidate_name = (candidate.group(1) or candidate.group(2) or "").strip()
                if re.fullmatch(r'\d{2}-\d{2}-\d{4}', candidate_name):
                    continue
                project_name = normalize_name(candidate_name)
                break

            title = raw_work
            title = _remove_unquoted_matches(title, QUICK_TAG_RE)
            title = _remove_unquoted_matches(title, PROJ_RE)
            title = _remove_unquoted_matches(title, TIME_TOKEN_RE)
            title = _unquote_title(title)
            title = re.sub(r'\s+', ' ', title).strip(" -_,.;:")

            if not title:
                title = raw or "(sin asunto)"

            due_date = payload.get("due_date") or detected_due_date
            due_time = detected_due_time

            project_id = None
            if project_name:
                project_id = find_project_by_name_active(project_name)
                if project_id is None:
                    project_id = exec_sql(
                        "INSERT INTO projects(name, archived) VALUES(%s, %s)",
                        (project_name, 0),
                    )

            tag_names = []
            seen_tags = set()
            for t in tags + ["inbox.gmail"]:
                t_norm = normalize_name(t)
                if not t_norm:
                    continue
                low = t_norm.lower()
                if low in seen_tags:
                    continue
                seen_tags.add(low)
                tag_names.append(t_norm)

            task_id = exec_sql(
                "INSERT INTO tasks(title, notes, project_id, folder_id, due_date, due_time, recurrence_rule) "
                "VALUES(%s,%s,%s,NULL,%s,%s,%s)",
                (
                    title,
                    payload["notes"],
                    project_id,
                    due_date,
                    due_time,
                    recurrence,
                ),
            )

            for tname in tag_names:
                tid = get_or_create_tag(tname)
                exec_sql(
                    "INSERT IGNORE INTO task_tags(task_id, tag_id) VALUES(%s,%s)",
                    (task_id, tid),
                )

            exec_sql(
                "INSERT INTO imported_emails(gmail_message_id, gmail_thread_id, task_id) "
                "VALUES(%s,%s,%s)",
                (
                    payload["gmail_message_id"],
                    payload["gmail_thread_id"],
                    task_id,
                ),
            )

            created += 1

        commit()
        flash(f"Importación Gmail completada: {created} tareas creadas, {skipped} omitidas.", "ok")

    except Exception as e:
        rollback()
        err = str(e)
        if "disabled_client" in err:
            flash(
                "OAuth de Google deshabilitado (disabled_client). "
                "Activa el OAuth Client ID en Google Cloud Console o crea uno nuevo, "
                "descarga de nuevo el JSON y reemplaza instance/gmail_credentials.json. "
                "Después borra instance/gmail_token.json e intenta importar otra vez.",
                "error",
            )
        elif _is_google_invalid_grant_error(e):
            flash(
                "No se pudieron importar los correos: token de Google caducado o revocado (invalid_grant). "
                "Ve a Admin > Renovación tokens Google y pulsa 'Renovar token'.",
                "error",
            )
        else:
            flash(f"No se pudieron importar los correos: {e}", "error")

    return redirect(next_url)


@app.route("/calendar/import_to_inbox", methods=["POST"])
def calendar_import_to_inbox():
    next_url = safe_next_url(request.form.get("next"), "home")

    try:
        ensure_imported_calendar_events_table()

        creds_path = google_credentials_path()
        token_path = google_token_path()

        if not creds_path.exists():
            flash("Falta instance/gmail_credentials.json.", "error")
            return redirect(next_url)

        service = build_google_service(
            creds_path,
            token_path,
            api_name="calendar",
            api_version="v3",
        )

        import_mode = (request.form.get("import_mode") or "event_date").strip()
        range_value = (request.form.get("range_value") or "today").strip()
        # Calendario objetivo compartido definido en configuración.
        calendar_id = calendar_sync_calendar_id()

        if import_mode == "created_date":
            events = list_recent_events_by_created(
                service,
                calendar_id=calendar_id,
                created_range=range_value,
            )
        else:
            events = list_upcoming_events(
                service,
                calendar_id=calendar_id,
                days_range=range_value,
            )

        if not events:
            flash("No se encontraron eventos para importar.", "ok")
            return redirect(next_url)

        tag_calendar_id = get_or_create_tag("inbox.calendar")
        tag_agenda_id = get_or_create_tag(role_tag("agenda"))

        created = 0
        skipped = 0

        for ev in events:
            event_id = ev.get("id")
            if not event_id:
                skipped += 1
                continue

            if calendar_event_already_imported(event_id):
                skipped += 1
                continue

            payload = _google_event_to_task_fields(ev)
            payload["google_event_id"] = ev.get("id")
            payload["google_calendar_id"] = calendar_id

            # Parseo rápido también en títulos importados desde Calendar.
            parsed_title, parsed_tags, quick_project_name, quick_folder_name = parse_task_quick_entry(
                payload.get("title") or ""
            )
            final_title = parsed_title or (payload.get("title") or "(sin título)")

            project_id = None
            folder_id = None

            if quick_folder_name:
                folder_id = find_folder_by_name(quick_folder_name)
            elif quick_project_name:
                project_id = find_project_by_name_active(quick_project_name)
                if project_id is None:
                    project_id = exec_sql(
                        "INSERT INTO projects(name, archived) VALUES(%s, %s)",
                        (quick_project_name, 0),
                    )

            task_id = exec_sql(
                "INSERT INTO tasks("
                "title, notes, project_id, folder_id, due_date, due_time, recurrence_rule, "
                "google_event_id, google_calendar_id, google_event_etag, "
                "calendar_remote_updated_at, calendar_sync_state, calendar_last_synced_at"
                ") "
                "VALUES(%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s,%s,'synced',NOW())",
                (
                    final_title,
                    payload["notes"],
                    project_id,
                    folder_id,
                    payload["due_date"],
                    payload["due_time"],
                    payload["google_event_id"],
                    payload["google_calendar_id"],
                    payload.get("google_event_etag"),
                    payload.get("calendar_remote_updated_at"),
                ),
            )

            exec_sql(
                "INSERT IGNORE INTO task_tags(task_id, tag_id) VALUES(%s,%s)",
                (task_id, tag_calendar_id),
            )
            exec_sql(
                "INSERT IGNORE INTO task_tags(task_id, tag_id) VALUES(%s,%s)",
                (task_id, tag_agenda_id),
            )

            for t in parsed_tags:
                tag_id = get_or_create_tag(t)
                exec_sql(
                    "INSERT IGNORE INTO task_tags(task_id, tag_id) VALUES(%s,%s)",
                    (task_id, tag_id),
                )

            exec_sql(
                "INSERT INTO imported_calendar_events(google_event_id, task_id) VALUES(%s,%s)",
                (payload["google_event_id"], task_id),
            )

            created += 1

        commit()

        range_labels = {
            "today": "hoy",
            "7days": "7 días",
            "15days": "15 días",
        }
        range_txt = range_labels.get(range_value, range_value)

        if import_mode == "created_date":
            flash(
                f"Importación Calendar por fecha de creación ({range_txt}): "
                f"{created} tareas creadas, {skipped} omitidas.",
                "ok",
            )
        else:
            flash(
                f"Importación Calendar por fecha del evento ({range_txt}): "
                f"{created} tareas creadas, {skipped} omitidas.",
                "ok",
            )

    except Exception as e:
        rollback()
        if _is_google_invalid_grant_error(e):
            flash(
                "No se pudieron importar los eventos: token de Google caducado o revocado (invalid_grant). "
                "Ve a Admin > Renovación tokens Google y pulsa 'Renovar token'.",
                "error",
            )
        else:
            flash(f"No se pudieron importar los eventos: {e}", "error")

    return redirect(next_url)




# Ruta temporal para depuración de variables de entorno
@app.route("/debug/env")
def debug_env():
    import os
    return "<pre>" + "\n".join(f"{k}={v}" for k, v in os.environ.items()) + "</pre>"
    import os
    return "<pre>" + "\n".join(f"{k}={v}" for k, v in os.environ.items()) + "</pre>"


@app.route("/calendar/sync_now", methods=["POST"])
def calendar_sync_now():

    next_url = safe_next_url(request.form.get("next"), "home")

    try:
        service = _calendar_sync_service()
        if service is None:
            msg = "No hay credenciales de Google Calendar disponibles."
            session["calendar_sync_last_info"] = msg
            session["calendar_sync_last_level"] = "error"
            flash(msg, "error")
            return redirect(next_url)

        import_mode = (request.form.get("import_mode") or "event_date").strip().lower()
        range_value = (request.form.get("range_value") or "15days").strip().lower()

        if import_mode not in {"event_date", "created_date"}:
            import_mode = "event_date"
        if range_value not in {"today", "7days", "15days"}:
            range_value = "15days"

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

        partial = " (parcial por límite de tiempo/páginas)" if pull_res.get("truncated") else ""
        mode_txt = "fecha de creación" if import_mode == "created_date" else "fecha del evento"
        range_labels = {
            "today": "hoy",
            "7days": "7 días",
            "15days": "15 días",
        }
        range_txt = range_labels.get(range_value, range_value)

        msg = (
            "Sync Calendar: "
            f"en GTD (updated={pull_res['updated']}, pendientes_revisar={pull_res.get('imported', 0)}, conflicts={pull_res['conflicts']}, archived={pull_res['archived']}) "
            f"en GCalendar (ok={push_res['ok']}, fail={push_res['fail']}) "
            f"[detección no enlazados: {mode_txt}, {range_txt}].{partial}"
        )
        session["calendar_sync_last_info"] = msg
        session["calendar_sync_last_level"] = "ok"
        flash(msg, "ok")
    except Exception as e:
        rollback()
        if _is_google_invalid_grant_error(e):
            msg = (
                "No se pudo sincronizar con Google Calendar: token caducado o revocado (invalid_grant). "
                "Renueva el token en Admin > Renovación tokens Google."
            )
        else:
            msg = f"No se pudo sincronizar con Google Calendar: {e}"
        session["calendar_sync_last_info"] = msg
        session["calendar_sync_last_level"] = "error"
        flash(msg, "error")

    return redirect(next_url)
