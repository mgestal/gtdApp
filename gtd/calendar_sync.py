"""Sincronización bidireccional de tareas con Google Calendar (push, pull, conflictos, autosync)."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from calendar_import import (
    build_google_service,
    list_recent_events_by_created,
    list_upcoming_events,
)

from gtd.config import BASE_DIR, load_config, save_config
from gtd.db import commit, exec_sql, q, q1, rollback
from gtd.lookups import find_folder_by_name, find_project_by_name_active, get_or_create_tag
from gtd.roles import role_tag
from gtd.quick_entry import parse_task_quick_entry
from gtd.gmail import _is_google_invalid_grant_error


def google_credentials_path() -> Path:
    return BASE_DIR / "instance" / "gmail_credentials.json"


def google_token_path() -> Path:
    return BASE_DIR / "instance" / "gmail_token.json"


def ensure_imported_calendar_events_table() -> None:
    exec_sql(
        "CREATE TABLE IF NOT EXISTS imported_calendar_events ("
        "id INT AUTO_INCREMENT PRIMARY KEY, "
        "google_event_id VARCHAR(255) NOT NULL UNIQUE, "
        "task_id INT NOT NULL, "
        "imported_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "INDEX idx_imported_calendar_events_task_id (task_id)"
        ")"
    )
    commit()

def ensure_calendar_pending_events_table() -> None:
    exec_sql(
        "CREATE TABLE IF NOT EXISTS calendar_pending_events ("
        "id INT AUTO_INCREMENT PRIMARY KEY, "
        "google_event_id VARCHAR(255) NOT NULL UNIQUE, "
        "google_calendar_id VARCHAR(500), "
        "title VARCHAR(500), "
        "due_date DATE NULL, "
        "due_time TIME NULL, "
        "notes TEXT, "
        "event_data TEXT, "
        "discovered_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "state ENUM('pending','imported','ignored') NOT NULL DEFAULT 'pending', "
        "task_id INT NULL, "
        "INDEX idx_cpe_state (state)"
        ")"
    )
    commit()


def calendar_event_already_imported(event_id: str) -> bool:
    row = q1(
        "SELECT id FROM imported_calendar_events WHERE google_event_id=%s",
        (event_id,),
    )
    return row is not None


DEFAULT_CAL_SYNC_CALENDAR_ID = "mgestal@gmail.com"
CAL_SYNC_TIMEZONE = "Europe/Madrid"
CAL_SYNC_DEFAULT_DURATION_MINUTES = 30
CAL_SYNC_COOLDOWN_SECONDS = 120
CAL_SYNC_AUTO_ENABLED = False
CAL_SYNC_DB_LOCK_NAME = "gtdapp_calendar_sync"
_calendar_last_auto_sync: Optional[datetime] = None


def calendar_sync_calendar_id() -> str:
    try:
        cfg = load_config()
        value = (((cfg.get("app") or {}).get("calendar_sync") or {}).get("calendar_id") or "").strip()
        if value:
            return value
    except Exception:
        pass
    return DEFAULT_CAL_SYNC_CALENDAR_ID


def _is_retryable_db_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "(1020" in text
        or "record has changed since last read" in text
        or "deadlock" in text
        or "lock wait timeout" in text
    )


def _calendar_sync_lock_acquire(timeout_seconds: int = 1) -> bool:
    row = q1("SELECT GET_LOCK(%s, %s) AS ok", (CAL_SYNC_DB_LOCK_NAME, timeout_seconds))
    return bool(row and int(row.get("ok") or 0) == 1)


def _calendar_sync_lock_release() -> None:
    try:
        q1("SELECT RELEASE_LOCK(%s) AS ok", (CAL_SYNC_DB_LOCK_NAME,))
    except Exception:
        pass


def _parse_google_dt(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _calendar_sync_service():
    creds_path = google_credentials_path()
    token_path = google_token_path()
    if not creds_path.exists() or not token_path.exists():
        return None
    if not os.access(creds_path, os.R_OK):
        raise RuntimeError(
            f"No se puede leer {creds_path}. Revisa propietario/permisos para el usuario del servidor web."
        )
    if not os.access(token_path, os.R_OK):
        raise RuntimeError(
            f"No se puede leer {token_path}. Revisa propietario/permisos para el usuario del servidor web."
        )

    prev = os.environ.get("GTD_NON_INTERACTIVE_OAUTH")
    os.environ["GTD_NON_INTERACTIVE_OAUTH"] = "1"
    try:
        return build_google_service(
            creds_path,
            token_path,
            api_name="calendar",
            api_version="v3",
        )
    except RuntimeError:
        raise  # propaga errores de scopes/autenticación con su mensaje real
    except Exception as e:
        if _is_google_invalid_grant_error(e):
            raise RuntimeError(
                "Token Google caducado o revocado (invalid_grant). "
                "Renueva el token en Admin > Renovación tokens Google."
            )
        return None
    finally:
        if prev is None:
            os.environ.pop("GTD_NON_INTERACTIVE_OAUTH", None)
        else:
            os.environ["GTD_NON_INTERACTIVE_OAUTH"] = prev


def _task_calendar_row(task_id: int) -> Optional[Dict[str, Any]]:
    return q1(
        "SELECT id, title, notes, due_date, due_time, recurrence_rule, completed_at, archived, "
        "google_event_id, google_calendar_id, google_event_etag, "
        "calendar_sync_state, calendar_sync_ignored, calendar_sync_error, calendar_last_synced_hash, "
        "calendar_last_synced_at, calendar_local_changed_at, calendar_remote_updated_at "
        "FROM tasks WHERE id=%s",
        (task_id,),
    )


def _task_calendar_hash(row: Dict[str, Any]) -> str:
    # Campos realmente sincronizados con el contenido del evento.
    payload = {
        "title": row.get("title") or "",
        "notes": row.get("notes") or "",
        "due_date": str(row.get("due_date") or ""),
        "due_time": str(row.get("due_time") or ""),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _task_calendar_relevant_changed(task_id: int) -> bool:
    """True when local edits changed fields that are actually synced with Google Calendar."""
    row = _task_calendar_row(task_id)
    if not row:
        return False
    last_hash = (row.get("calendar_last_synced_hash") or "").strip()
    if not last_hash:
        # Sin baseline no podemos distinguir; asumimos que sí hubo cambio relevante.
        return True
    return _task_calendar_hash(row) != last_hash


def _task_has_calendar_datetime(row: Dict[str, Any]) -> bool:
    return bool(row.get("due_date") or row.get("due_time"))


def _task_to_gcal_event(row: Dict[str, Any]) -> Dict[str, Any]:
    tz = ZoneInfo(CAL_SYNC_TIMEZONE)
    now_local = datetime.now(tz)
    due_date = row.get("due_date")
    due_time = row.get("due_time")

    # MySQL TIME puede llegar como datetime.timedelta (según driver/configuración).
    if isinstance(due_time, timedelta):
        total_seconds = int(due_time.total_seconds())
        total_seconds = total_seconds % (24 * 3600)
        hh = total_seconds // 3600
        mm = (total_seconds % 3600) // 60
        ss = total_seconds % 60
        due_time = time(hour=hh, minute=mm, second=ss)

    if not due_date and due_time:
        due_date = now_local.date()

    event: Dict[str, Any] = {
        "summary": (row.get("title") or "(sin título)").strip(),
        "description": (row.get("notes") or "").strip() or None,
    }

    if due_date and due_time:
        start_local = datetime.combine(due_date, due_time, tzinfo=tz)
        end_local = start_local + timedelta(minutes=CAL_SYNC_DEFAULT_DURATION_MINUTES)
        event["start"] = {"dateTime": start_local.isoformat(), "timeZone": CAL_SYNC_TIMEZONE}
        event["end"] = {"dateTime": end_local.isoformat(), "timeZone": CAL_SYNC_TIMEZONE}
    elif due_date:
        next_day = due_date + timedelta(days=1)
        event["start"] = {"date": due_date.isoformat()}
        event["end"] = {"date": next_day.isoformat()}
    else:
        # Fallback para tareas nuevas sin fecha/hora: evento de día completo hoy.
        fallback_date = now_local.date()
        next_day = fallback_date + timedelta(days=1)
        event["start"] = {"date": fallback_date.isoformat()}
        event["end"] = {"date": next_day.isoformat()}

    desc = event.get("description") or ""
    marker = f"\n\n[GTD_TASK_ID={row['id']}]"
    event["description"] = (desc + marker).strip()
    return event


def _google_event_to_task_fields(ev: Dict[str, Any]) -> Dict[str, Any]:
    summary = (ev.get("summary") or "").strip()
    description = (ev.get("description") or "").strip() or None

    start = ev.get("start") or {}
    due_date = None
    due_time = None

    if start.get("date"):
        due_date = datetime.strptime(start.get("date"), "%Y-%m-%d").date()
        due_time = None
    elif start.get("dateTime"):
        tz = ZoneInfo(CAL_SYNC_TIMEZONE)
        dt = datetime.fromisoformat(start.get("dateTime").replace("Z", "+00:00")).astimezone(tz)
        due_date = dt.date()
        due_time = dt.timetz().replace(tzinfo=None)

    return {
        "title": summary,
        "notes": description,
        "due_date": due_date,
        "due_time": due_time,
        "google_event_etag": ev.get("etag"),
        "calendar_remote_updated_at": _parse_google_dt(ev.get("updated")),
    }


def _mark_task_calendar_dirty(task_id: int, force_push_if_empty: bool = False) -> None:
    row = _task_calendar_row(task_id)
    if not row:
        return

    has_when = _task_has_calendar_datetime(row)
    has_remote = bool(row.get("google_event_id"))
    archived = int(row.get("archived") or 0) == 1

    if int(row.get("calendar_sync_ignored") or 0) == 1 and not has_remote:
        exec_sql(
            "UPDATE tasks SET calendar_sync_state='none', calendar_sync_error=NULL, "
            "calendar_local_changed_at=NULL WHERE id=%s",
            (task_id,),
        )
        return

    if archived:
        state = "pending_delete" if has_remote else "none"
    elif not has_when:
        state = "pending_push" if (has_remote or force_push_if_empty) else "none"
    else:
        state = "pending_push"

    exec_sql(
        "UPDATE tasks "
        "SET calendar_local_changed_at=NOW(), calendar_sync_state=%s "
        "WHERE id=%s",
        (state, task_id),
    )


def _sync_task_push(task_id: int, service=None, force_remote_update: bool = False) -> bool:
    row = _task_calendar_row(task_id)
    if not row:
        return True

    if row.get("calendar_sync_state") == "conflict":
        return False

    # Si solo cambió metadata local no sincronizada (carpeta/proyecto/etiquetas),
    # no hay nada que empujar a Google Calendar.
    event_id = row.get("google_event_id")
    last_hash = (row.get("calendar_last_synced_hash") or "").strip()
    current_hash = _task_calendar_hash(row)
    if (
        not force_remote_update
        and
        row.get("calendar_sync_state") == "pending_push"
        and event_id
        and last_hash
        and current_hash == last_hash
    ):
        exec_sql(
            "UPDATE tasks "
            "SET calendar_sync_state='synced', calendar_sync_error=NULL, calendar_last_synced_at=NOW(), calendar_local_changed_at=NULL "
            "WHERE id=%s",
            (task_id,),
        )
        return True

    # Regla de negocio: sin fecha y sin hora no se sincroniza a Calendar.
    # Si ya existia evento remoto vinculado, se elimina para mantener consistencia.
    has_when = _task_has_calendar_datetime(row)
    archived = int(row.get("archived") or 0) == 1
    if not archived and not has_when:
        event_id = row.get("google_event_id")
        calendar_id = row.get("google_calendar_id") or calendar_sync_calendar_id()

        if event_id:
            own_service = False
            if service is None:
                service = _calendar_sync_service()
                own_service = True

            if service is None:
                exec_sql(
                    "UPDATE tasks SET calendar_sync_error=%s, calendar_sync_state='error' WHERE id=%s",
                    ("No hay servicio de Google Calendar disponible", task_id),
                )
                return False

            try:
                service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
            except Exception as e:
                exec_sql(
                    "UPDATE tasks SET calendar_sync_error=%s, calendar_sync_state='error' WHERE id=%s",
                    (str(e), task_id),
                )
                return False
            finally:
                if own_service:
                    pass

        exec_sql(
            "UPDATE tasks "
            "SET google_event_id=NULL, google_event_etag=NULL, google_calendar_id=%s, "
            "calendar_sync_state='none', calendar_sync_error=NULL, "
            "calendar_last_synced_hash=%s, calendar_last_synced_at=NOW(), calendar_local_changed_at=NULL "
            "WHERE id=%s",
            (calendar_id, _task_calendar_hash(row), task_id),
        )
        return True

    own_service = False
    if service is None:
        service = _calendar_sync_service()
        own_service = True

    if service is None:
        exec_sql(
            "UPDATE tasks SET calendar_sync_error=%s, calendar_sync_state='error' WHERE id=%s",
            ("No hay servicio de Google Calendar disponible", task_id),
        )
        return False

    try:
        calendar_id = row.get("google_calendar_id") or calendar_sync_calendar_id()
        event_id = row.get("google_event_id")
        body = _task_to_gcal_event(row)

        if int(row.get("archived") or 0) == 1:
            if event_id:
                service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
            exec_sql(
                "UPDATE tasks "
                "SET google_event_id=NULL, google_event_etag=NULL, google_calendar_id=%s, "
                "calendar_sync_state='none', calendar_sync_error=NULL, "
                "calendar_last_synced_hash=%s, calendar_last_synced_at=NOW(), calendar_local_changed_at=NULL "
                "WHERE id=%s",
                (calendar_id, _task_calendar_hash(row), task_id),
            )
            return True

        if not body:
            return True

        if event_id:
            ev = service.events().update(calendarId=calendar_id, eventId=event_id, body=body).execute()
        else:
            ev = service.events().insert(calendarId=calendar_id, body=body).execute()

        new_hash = _task_calendar_hash(row)
        exec_sql(
            "UPDATE tasks "
            "SET google_event_id=%s, google_calendar_id=%s, google_event_etag=%s, "
            "calendar_remote_updated_at=%s, calendar_sync_state='synced', calendar_sync_error=NULL, "
            "calendar_last_synced_hash=%s, calendar_last_synced_at=NOW(), calendar_local_changed_at=NULL "
            "WHERE id=%s",
            (
                ev.get("id"),
                calendar_id,
                ev.get("etag"),
                _parse_google_dt(ev.get("updated")),
                new_hash,
                task_id,
            ),
        )
        return True
    except Exception as e:
        exec_sql(
            "UPDATE tasks SET calendar_sync_error=%s, calendar_sync_state='error' WHERE id=%s",
            (str(e), task_id),
        )
        return False
    finally:
        if own_service:
            pass


def run_calendar_push_sync(limit: int = 100, service=None) -> Dict[str, int]:
    rows = q(
        "SELECT id FROM tasks "
        "WHERE calendar_sync_state IN ('pending_push','pending_delete','error') "
        "ORDER BY id ASC LIMIT %s",
        (limit,),
    )
    ok = 0
    fail = 0
    for r in rows:
        if _sync_task_push(int(r["id"]), service=service):
            ok += 1
        else:
            fail += 1
    return {"ok": ok, "fail": fail, "total": len(rows)}


def _apply_google_to_task(task_id: int, ev: Dict[str, Any]) -> None:
    fields = _google_event_to_task_fields(ev)

    # Si el payload remoto llega incompleto (sin start/end), evitamos perder fecha/hora local.
    if fields.get("due_date") is None and fields.get("due_time") is None:
        current = _task_calendar_row(task_id)
        if current:
            fields["due_date"] = current.get("due_date")
            fields["due_time"] = current.get("due_time")

    exec_sql(
        "UPDATE tasks "
        "SET title=%s, notes=%s, due_date=%s, due_time=%s, "
        "google_event_etag=%s, calendar_remote_updated_at=%s, "
        "calendar_sync_state='synced', calendar_sync_error=NULL, "
        "calendar_last_synced_at=NOW(), calendar_local_changed_at=NULL "
        "WHERE id=%s",
        (
            fields["title"],
            fields["notes"],
            fields["due_date"],
            fields["due_time"],
            fields["google_event_etag"],
            fields["calendar_remote_updated_at"],
            task_id,
        ),
    )
    row = _task_calendar_row(task_id)
    if row:
        exec_sql(
            "UPDATE tasks SET calendar_last_synced_hash=%s WHERE id=%s",
            (_task_calendar_hash(row), task_id),
        )


def _register_calendar_conflict(task_id: int, ev: Dict[str, Any]) -> None:
    exec_sql(
        "UPDATE tasks "
        "SET calendar_sync_state='conflict', calendar_conflict_payload=%s, calendar_conflict_at=NOW(), calendar_sync_error=NULL "
        "WHERE id=%s",
        (json.dumps(ev, ensure_ascii=False), task_id),
    )


def _create_task_from_calendar_event(ev: Dict[str, Any], calendar_id: str) -> Optional[int]:
    """Create a GTD task from a Google Calendar event when it is not linked yet."""
    event_id = (ev.get("id") or "").strip()
    if not event_id:
        return None
    if (ev.get("status") or "").strip().lower() == "cancelled":
        return None

    already = q1("SELECT id FROM tasks WHERE google_event_id=%s", (event_id,))
    if already:
        return int(already["id"])

    payload = _google_event_to_task_fields(ev)
    payload["google_event_id"] = event_id
    payload["google_calendar_id"] = calendar_id

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

    # Etiquetas por defecto de importación Calendar.
    tag_calendar_id = get_or_create_tag("inbox.calendar")
    tag_agenda_id = get_or_create_tag(role_tag("agenda"))
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

    ensure_imported_calendar_events_table()
    exec_sql(
        "INSERT IGNORE INTO imported_calendar_events(google_event_id, task_id) VALUES(%s,%s)",
        (payload["google_event_id"], task_id),
    )

    row = _task_calendar_row(task_id)
    if row:
        exec_sql(
            "UPDATE tasks SET calendar_last_synced_hash=%s WHERE id=%s",
            (_task_calendar_hash(row), task_id),
        )

    return task_id


def _stage_calendar_event_for_review(ev: Dict[str, Any], calendar_id: str) -> bool:
    """Stage a discovered GCal event for user review instead of auto-creating a GTD task."""
    event_id = (ev.get("id") or "").strip()
    if not event_id or (ev.get("status") or "").strip().lower() == "cancelled":
        return False
    if q1("SELECT id FROM tasks WHERE google_event_id=%s", (event_id,)):
        return False
    ensure_calendar_pending_events_table()
    payload = _google_event_to_task_fields(ev)
    exec_sql(
        "INSERT INTO calendar_pending_events "
        "(google_event_id, google_calendar_id, title, due_date, due_time, notes, event_data, state) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending') "
        "ON DUPLICATE KEY UPDATE "
        "title=VALUES(title), due_date=VALUES(due_date), due_time=VALUES(due_time), "
        "notes=VALUES(notes), event_data=VALUES(event_data), "
        "state=IF(state IN ('imported','ignored'),state,'pending')",
        (
            event_id,
            calendar_id,
            (payload.get("title") or "")[:500],
            payload.get("due_date"),
            payload.get("due_time"),
            (payload.get("notes") or "")[:65535],
            json.dumps(ev),
        ),
    )
    return True


def run_calendar_pull_sync(
    force: bool = False,
    service=None,
    max_pages: Optional[int] = None,
    time_budget_seconds: Optional[int] = None,
    discover_mode: str = "event_date",
    discover_range: Optional[str] = None,
    stage_only: bool = False,
) -> Dict[str, int]:
    global _calendar_last_auto_sync

    if not force and _calendar_last_auto_sync is not None:
        elapsed = (datetime.now() - _calendar_last_auto_sync).total_seconds()
        if elapsed < CAL_SYNC_COOLDOWN_SECONDS:
            return {"updated": 0, "conflicts": 0, "archived": 0, "seen": 0, "skipped": 1}

    own_service = False
    if service is None:
        service = _calendar_sync_service()
        own_service = True

    if service is None:
        return {"updated": 0, "conflicts": 0, "archived": 0, "seen": 0, "skipped": 0}

    # En sync manual (force=True) evitamos paginar todo el calendario:
    # leemos directamente los eventos vinculados en GTD por google_event_id.
    if force:
        updated = 0
        conflicts = 0
        archived = 0
        imported = 0
        seen = 0

        linked_rows = q(
            "SELECT id, google_event_id, calendar_last_synced_at, calendar_local_changed_at "
            "FROM tasks "
            "WHERE google_event_id IS NOT NULL "
            "ORDER BY id ASC LIMIT 1000"
        )

        for row in linked_rows:
            task_id = int(row["id"])
            eid = row.get("google_event_id")
            if not eid:
                continue

            seen += 1
            try:
                ev = service.events().get(
                    calendarId=calendar_sync_calendar_id(),
                    eventId=eid,
                ).execute()
            except Exception as e:
                msg = str(e)
                msg_low = msg.lower()
                if "404" in msg_low or "not found" in msg_low or "410" in msg_low or "gone" in msg_low:
                    exec_sql(
                        "UPDATE tasks "
                        "SET archived=1, archived_at=NOW(), "
                        "google_event_id=NULL, google_event_etag=NULL, calendar_sync_state='remote_deleted', "
                        "calendar_sync_error='Evento borrado en Google Calendar' "
                        "WHERE id=%s",
                        (task_id,),
                    )
                    archived += 1
                else:
                    exec_sql(
                        "UPDATE tasks SET calendar_sync_state='error', calendar_sync_error=%s WHERE id=%s",
                        (msg[:1000], task_id),
                    )
                continue

            if ev.get("status") == "cancelled":
                exec_sql(
                    "UPDATE tasks "
                    "SET archived=1, archived_at=NOW(), "
                    "google_event_id=NULL, google_event_etag=NULL, calendar_sync_state='remote_deleted', "
                    "calendar_sync_error='Evento borrado en Google Calendar' "
                    "WHERE id=%s",
                    (task_id,),
                )
                archived += 1
                continue

            local_changed = False
            if row.get("calendar_local_changed_at"):
                if not row.get("calendar_last_synced_at"):
                    local_changed = True
                else:
                    local_changed = row["calendar_local_changed_at"] > row["calendar_last_synced_at"]

            if local_changed:
                if _task_calendar_relevant_changed(task_id):
                    _register_calendar_conflict(task_id, ev)
                    conflicts += 1
                    continue
                # Cambio local no sincronizable (p.ej. carpeta/proyecto/etiquetas):
                # aplicamos Google sin abrir conflicto.

            _apply_google_to_task(task_id, ev)
            updated += 1

        # También importar eventos no enlazados aún, reutilizando el mismo criterio
        # del desplegable de Inbox cuando se recibe desde /calendar/sync_now.
        safe_mode = (discover_mode or "event_date").strip().lower()
        safe_range = (discover_range or "15days").strip().lower()
        if safe_range not in {"today", "7days", "15days"}:
            safe_range = "15days"

        if safe_mode == "created_date":
            discover_events = list_recent_events_by_created(
                service,
                calendar_id=calendar_sync_calendar_id(),
                created_range=safe_range,
            )
        else:
            discover_events = list_upcoming_events(
                service,
                calendar_id=calendar_sync_calendar_id(),
                days_range=safe_range,
            )

        for ev in discover_events:
            if stage_only:
                if _stage_calendar_event_for_review(ev, calendar_sync_calendar_id()):
                    imported += 1
            else:
                tid = _create_task_from_calendar_event(ev, calendar_sync_calendar_id())
                if tid:
                    imported += 1

        _calendar_last_auto_sync = datetime.now()
        return {
            "updated": updated,
            "conflicts": conflicts,
            "archived": archived,
            "imported": imported,
            "seen": seen,
            "skipped": 0,
            "pages": 0,
            "truncated": 0,
        }

    updated = 0
    conflicts = 0
    archived = 0
    imported = 0
    seen = 0
    pages = 0
    truncated = 0
    started_at = datetime.now()

    cfg = load_config()
    cal_cfg = cfg.setdefault("app", {}).setdefault("calendar_sync", {})
    updated_min = cal_cfg.get("last_pull_utc")

    page_token = None
    last_updated_seen = updated_min

    while True:
        if max_pages is not None and pages >= max_pages:
            truncated = 1
            break
        if time_budget_seconds is not None:
            elapsed = (datetime.now() - started_at).total_seconds()
            if elapsed >= time_budget_seconds:
                truncated = 1
                break

        params = {
            "calendarId": calendar_sync_calendar_id(),
            "showDeleted": True,
            "singleEvents": True,
            "maxResults": 250,
        }
        if updated_min:
            params["updatedMin"] = updated_min
        if page_token:
            params["pageToken"] = page_token

        resp = service.events().list(**params).execute()
        pages += 1
        events = resp.get("items", [])

        for ev in events:
            seen += 1
            eid = ev.get("id")
            if not eid:
                continue

            row = q1(
                "SELECT id, recurrence_rule, calendar_sync_state, calendar_last_synced_at, calendar_local_changed_at "
                "FROM tasks WHERE google_event_id=%s",
                (eid,),
            )
            if not row:
                tid = _create_task_from_calendar_event(ev, calendar_sync_calendar_id())
                if tid:
                    imported += 1
                continue

            task_id = int(row["id"])
            if ev.get("updated") and (not last_updated_seen or ev.get("updated") > last_updated_seen):
                last_updated_seen = ev.get("updated")

            if ev.get("status") == "cancelled":
                exec_sql(
                    "UPDATE tasks "
                    "SET archived=1, archived_at=NOW(), "
                    "google_event_id=NULL, google_event_etag=NULL, calendar_sync_state='remote_deleted', "
                    "calendar_sync_error='Evento borrado en Google Calendar' "
                    "WHERE id=%s",
                    (task_id,),
                )
                archived += 1
                continue

            local_changed = False
            if row.get("calendar_local_changed_at"):
                if not row.get("calendar_last_synced_at"):
                    local_changed = True
                else:
                    local_changed = row["calendar_local_changed_at"] > row["calendar_last_synced_at"]

            if local_changed:
                if _task_calendar_relevant_changed(task_id):
                    _register_calendar_conflict(task_id, ev)
                    conflicts += 1
                    continue
                # Cambio local no sincronizable (p.ej. carpeta/proyecto/etiquetas):
                # aplicamos Google sin abrir conflicto.

            _apply_google_to_task(task_id, ev)
            updated += 1

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    if last_updated_seen:
        cal_cfg["last_pull_utc"] = last_updated_seen
        save_config(cfg)

    _calendar_last_auto_sync = datetime.now()
    return {
        "updated": updated,
        "conflicts": conflicts,
        "archived": archived,
        "imported": imported,
        "seen": seen,
        "skipped": 0,
        "pages": pages,
        "truncated": truncated,
    }


def maybe_run_calendar_autosync() -> None:
    if not CAL_SYNC_AUTO_ENABLED:
        return

    acquired = False
    try:
        acquired = _calendar_sync_lock_acquire(timeout_seconds=0)
        if not acquired:
            return

        pull_res = run_calendar_pull_sync(force=False)
        if pull_res.get("skipped"):
            return
        service = _calendar_sync_service()
        if service is None:
            return
        run_calendar_push_sync(limit=50, service=service)
        commit()
    except Exception:
        rollback()
    finally:
        if acquired:
            _calendar_sync_lock_release()
