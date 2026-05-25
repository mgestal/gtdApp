from __future__ import annotations

import subprocess
import json
import os
import re
import calendar
import hashlib
import unicodedata
import pymysql


from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for, send_file, jsonify, make_response
from markupsafe import Markup, escape as html_escape
from google_auth_oauthlib.flow import InstalledAppFlow

from urllib.parse import urlparse

import io
import csv
from xml.etree import ElementTree as ET
from werkzeug.utils import secure_filename

import functools

# --- Decorador para requerir token API en endpoints de extensión ---
def require_api_token(view_func):
    @functools.wraps(view_func)
    def wrapped(*args, **kwargs):
        # Permitir token en Authorization: Bearer <token> o en ?token= o JSON body
        token = None
        auth_header = request.headers.get("Authorization", "")
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
        if not token:
            token = request.args.get("token")
        if not token and request.is_json:
            token = (request.get_json(silent=True) or {}).get("token")
        if not token:
            return make_response(jsonify({"ok": False, "error": "Token requerido"}), 401)
        row = q1("SELECT user_id, active FROM api_tokens WHERE token=%s", (token,))
        if not row or not row.get("active") or int(row.get("user_id", 0)) != 1:
            return make_response(jsonify({"ok": False, "error": "Token inválido o revocado"}), 401)
        # Opcional: actualizar last_used_at
        exec_sql("UPDATE api_tokens SET last_used_at=NOW() WHERE token=%s", (token,))
        commit()
        return view_func(*args, **kwargs)
    return wrapped

from calendar_import import (
    build_google_service,
    list_upcoming_events,
    list_recent_events_by_created,
    event_to_task_payload,
)


from gmail_import import (
    build_gmail_service, 
    list_matching_messages,
    get_message_metadata, 
    message_to_task_payload
)

BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
CONFIG_PATH = INSTANCE_DIR / "config.json"

DEFAULT_CONFIG = {
    "db": {
        "host": "127.0.0.1",
        "port": 3306,
        "user": "gtd",
        "password": "gtd_password",
        "database": "gtd",
        "charset": "utf8mb4",
    },
    "app": {
        "timezone": "Europe/Madrid",
        "title": "GTD App",
        "calendar_sync": {
            "calendar_id": "mgestal@gmail.com",
        },
    },
}


BACKUP_DIR = BASE_DIR / "backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

# -------------------- Helpers comunes --------------------

def coerce_int(value: Any, default: int = 1, min_v: int = 1, max_v: Optional[int] = None) -> int:
    """Convierte a entero con límites y fallback seguro."""
    try:
        x = int(value)
    except Exception:
        return default
    if x < min_v:
        return min_v
    if max_v is not None and x > max_v:
        return max_v
    return x


def coerce_priority(value: Any, default: Optional[int] = None) -> Optional[int]:
    """Normaliza prioridad a 1=Alta, 2=Media, 3=Baja o None (sin prioridad)."""
    if value is None:
        return default
    raw = str(value).strip().lower()
    if raw == "":
        return None
    aliases = {
        "1": 1,
        "alta": 1,
        "high": 1,
        "2": 2,
        "media": 2,
        "medium": 2,
        "3": 3,
        "baja": 3,
        "low": 3,
    }
    if raw in aliases:
        return aliases[raw]
    return default


def get_page_arg(name: str = "page", default: int = 1) -> int:
    """Devuelve page desde query string con protección contra valores inválidos."""
    return coerce_int(request.args.get(name, default), default, min_v=1)


def get_pagination(total: int, per_page: int, page: int) -> Tuple[int, int, int]:
    """Calcula page, pages y offset para paginación."""
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(page, 1), pages)
    offset = (page - 1) * per_page
    return page, pages, offset


def safe_next_url(next_url: Optional[str], fallback_endpoint: str = "home", **fallback_values: Any) -> str:
    """
    Devuelve siempre una ruta interna relativa a la app, respetando script_root.
    Bloquea URLs absolutas externas.
    """
    script_root = (request.script_root or "").rstrip("/")
    fallback = url_for(fallback_endpoint, **fallback_values)

    next_url = (next_url or "").strip()
    if not next_url:
        return fallback

    p = urlparse(next_url)

    if p.scheme or p.netloc:
        return fallback

    if not next_url.startswith("/"):
        next_url = "/" + next_url

    if script_root and next_url != script_root and not next_url.startswith(script_root + "/"):
        next_url = script_root + next_url

    return next_url


RECURRENCE_PATTERNS = {
    r"\bcada\s+dia\b": "FREQ=DAILY;INTERVAL=1",
    r"\bcada\s+semana\b": "FREQ=WEEKLY;INTERVAL=1",
    r"\bcada\s+mes\b": "FREQ=MONTHLY;INTERVAL=1",
    r"\bcada\s+año\b": "FREQ=YEARLY;INTERVAL=1",
}


def safe_backup_filename(name: str) -> Optional[str]:
    """
    Acepta solo nombres tipo: gtd_20260303_121530.sql
    Sin barras, sin .., sin espacios raros.
    """
    name = (name or "").strip()
    if not name:
        return None
    if "/" in name or "\\" in name or ".." in name:
        return None
    if not name.lower().endswith(".sql"):
        return None
    # caracteres permitidos
    if not re.fullmatch(r"[A-Za-z0-9_\-\.]+", name):
        return None
    return name


def safe_archive_backup_filename(name: str) -> Optional[str]:
    """
    Acepta solo nombres de backup de archivo tipo: archive_tasks_YYYYmmdd_HHMMSS.json
    """
    name = (name or "").strip()
    if not name:
        return None
    if "/" in name or "\\" in name or ".." in name:
        return None
    if not re.fullmatch(r"archive_tasks_\d{8}_\d{6}\.json", name):
        return None
    return name


def _archive_scope_sql(scope: str) -> Tuple[str, Tuple[Any, ...], str]:
    scope = (scope or "all").strip().lower()
    if scope == "older_1m":
        return (
            " AND COALESCE(t.archived_at, t.completed_at, t.created_at) < (NOW() - INTERVAL 1 MONTH)",
            (),
            "más de 1 mes",
        )
    if scope == "before_current_year":
        return (
            " AND COALESCE(t.archived_at, t.completed_at, t.created_at) < MAKEDATE(YEAR(CURDATE()), 1)",
            (),
            "anteriores al año actual",
        )
    return ("", (), "todas")


def _dt_to_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "strftime"):
        try:
            return value.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return str(value)
    return str(value)


def _d_to_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "strftime"):
        try:
            return value.strftime("%Y-%m-%d")
        except Exception:
            return str(value)
    return str(value)


def _parse_date_or_none(value: Any) -> Optional[date]:
    raw = (str(value or "")).strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except Exception:
        return None


def _parse_time_or_none(value: Any) -> Optional[time]:
    raw = (str(value or "")).strip()
    if not raw:
        return None
    if len(raw) == 5:
        raw = raw + ":00"
    try:
        return time.fromisoformat(raw[:8])
    except Exception:
        return None


def _parse_datetime_or_none(value: Any) -> Optional[datetime]:
    raw = (str(value or "")).strip()
    if not raw:
        return None
    raw = raw.replace("T", " ")
    if raw.endswith("Z"):
        raw = raw[:-1]
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is not None:
            dt = dt.astimezone(ZoneInfo("Europe/Madrid")).replace(tzinfo=None)
        return dt
    except Exception:
        return None

def ensure_instance_config() -> None:
    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")

def load_config() -> Dict[str, Any]:
    ensure_instance_config()
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return DEFAULT_CONFIG

def save_config(cfg: Dict[str, Any]) -> None:
    ensure_instance_config()
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")



app = Flask(__name__, instance_relative_config=True)
app.config["SECRET_KEY"] = os.environ.get("GTD_SECRET_KEY", "CHANGE_ME_IN_PROD")

_schema_bootstrapped = False


# ---------------- Jinja2 filter: linkify_title ----------------

def _linkify_title(title: str) -> Markup:
    """Convierte URLs y enlaces Markdown en el título de una tarea a hipervínculos HTML.

    Sintaxis soportada:
      - URLs directas: www.ejemplo.com, http://..., https://...
      - Markdown: [Texto mostrado](https://ejemplo.com)
    """
    if not title:
        return Markup(html_escape(title or ""))

    _MD_LINK_RE = re.compile(r'\[([^\]]+)\]\(((?:https?://|www\.)[^\s\)]+)\)')
    _BARE_URL_RE = re.compile(r'(https?://[^\s<>"()\[\]]+|www\.[^\s<>"()\[\]]+)')

    matches: list = []
    for m in _MD_LINK_RE.finditer(title):
        matches.append((m.start(), m.end(), "md", m))

    md_ranges = [(s, e) for s, e, kind, _ in matches]
    for m in _BARE_URL_RE.finditer(title):
        s, e = m.start(), m.end()
        if not any(ms <= s and e <= me for ms, me in md_ranges):
            matches.append((s, e, "url", m))

    matches.sort(key=lambda x: x[0])

    parts: list = []
    last = 0
    for start, end, kind, m in matches:
        parts.append(str(html_escape(title[last:start])))
        if kind == "md":
            text = m.group(1)
            url = m.group(2)
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            parts.append(
                f'<a href="{html_escape(url)}" class="task-url-link" target="_blank" rel="noopener noreferrer">'
                f'{html_escape(text)}</a>'
            )
        else:
            url = m.group(0)
            href = url if url.startswith("http") else "https://" + url
            parts.append(
                f'<a href="{html_escape(href)}" class="task-url-link" target="_blank" rel="noopener noreferrer">'
                f'{html_escape(url)}</a>'
            )
        last = end

    parts.append(str(html_escape(title[last:])))
    return Markup("".join(parts))


app.jinja_env.filters["linkify_title"] = _linkify_title


# ---------------- Gmail import ----------------

def gmail_credentials_path() -> Path:
    return BASE_DIR / "instance" / "gmail_credentials.json"


def gmail_token_path() -> Path:
    return BASE_DIR / "instance" / "gmail_token.json"


def gmail_default_query() -> str:
    """
    Query por defecto para importar.
    Recomendación: usar una etiqueta específica, por ejemplo:
    label:ToGTD in:inbox
    """
    return os.environ.get("GTD_GMAIL_QUERY", "label:ToGTD in:inbox")


GOOGLE_OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar",
]


def _build_admin_google_flow(redirect_uri: str) -> InstalledAppFlow:
    return InstalledAppFlow.from_client_secrets_file(
        str(gmail_credentials_path()),
        GOOGLE_OAUTH_SCOPES,
        redirect_uri=redirect_uri,
    )


def _is_google_invalid_grant_error(err: Any) -> bool:
    txt = (str(err) or "").lower()
    return (
        "invalid_grant" in txt
        or "token has been expired or revoked" in txt
        or "token has expired or revoked" in txt
    )


def ensure_imported_emails_table() -> None:
    exec_sql(
        "CREATE TABLE IF NOT EXISTS imported_emails ("
        "id INT AUTO_INCREMENT PRIMARY KEY, "
        "gmail_message_id VARCHAR(255) NOT NULL UNIQUE, "
        "gmail_thread_id VARCHAR(255) NULL, "
        "task_id INT NOT NULL, "
        "imported_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "INDEX idx_imported_emails_task_id (task_id)"
        ")"
    )
    commit()


def gmail_message_already_imported(message_id: str) -> bool:
    row = q1(
        "SELECT id FROM imported_emails WHERE gmail_message_id=%s",
        (message_id,),
    )
    return row is not None



from urllib.parse import urlparse


# ---------------- GCalendar import ----------------


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
        "calendar_sync_state, calendar_sync_error, calendar_last_synced_hash, "
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
    tag_agenda_id = get_or_create_tag("agenda")
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
        "state=IF(state='imported','imported','pending')",
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



# -----------------------------------------------
# ---------------- DB helpers ----------------
# -----------------------------------------------

def get_db_conn():
    cfg = load_config()
    db = cfg.get("db", {})
    return pymysql.connect(
        host=db.get("host"),
        port=int(db.get("port", 3306)),
        user=db.get("user"),
        password=db.get("password"),
        database=db.get("database"),
        charset=db.get("charset", "utf8mb4"),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )

@app.before_request
def _before():
    g.cfg = load_config()
    ensure_schema_updates()
    maybe_run_calendar_autosync()


@app.route("/manifest.webmanifest")
def webmanifest():
    cfg = load_config()
    app_title = cfg.get("app", {}).get("title", "GTD App")
    script_root = (request.script_root or "").rstrip("/")
    base = script_root if script_root else ""

    start_url = f"{base}/pwa-launch" if base else "/pwa-launch"

    manifest = {
        "name": "GTD App",
        "short_name": "GTD App",
        "id": f"{base}/" if base else "/",
        "start_url": start_url,
        "scope": f"{base}/" if base else "/",
        "display": "standalone",
        "display_override": ["standalone", "minimal-ui"],
        "background_color": "#fff7f7",
        "theme_color": "#e7a7ad",
        "lang": "es",
        "description": "GTD App para gestionar tareas y proyectos.",
        "icons": [
            {
                "src": f"{base}/static/icons/emoji-icon-192.png?v=3",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": f"{base}/static/icons/emoji-icon-512.png?v=3",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any",
            },
        ],
    }

    response = app.response_class(
        json.dumps(manifest, ensure_ascii=False),
        mimetype="application/manifest+json",
    )
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/sw.js")
def service_worker():
    sw_path = BASE_DIR / "static" / "sw.js"
    response = send_file(sw_path, mimetype="application/javascript")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/pwa-launch", strict_slashes=False)
def pwa_launch():
    return render_template("pwa_launch.html")

@app.teardown_request
def _teardown(exc):
    conn = getattr(g, "db_conn", None)
    if conn:
        try:
            conn.close()
        except Exception:
            pass

def db():
    if "db_conn" not in g:
        g.db_conn = get_db_conn()
    return g.db_conn

def q(sql: str, params: Tuple[Any, ...] = ()) -> List[Dict[str, Any]]:
    with db().cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())

def q1(sql: str, params: Tuple[Any, ...] = ()) -> Optional[Dict[str, Any]]:
    rows = q(sql, params)
    return rows[0] if rows else None


def ensure_schema_updates() -> None:
    global _schema_bootstrapped
    if _schema_bootstrapped:
        return

    exec_sql(
        "ALTER TABLE tags "
        "ADD COLUMN IF NOT EXISTS type VARCHAR(80) NULL AFTER name"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS due_time TIME NULL AFTER due_date"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS archived TINYINT(1) NOT NULL DEFAULT 0 AFTER recurrence_rule"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS archived_at DATETIME NULL AFTER archived"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS deleted_at DATETIME NULL AFTER archived_at"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS deleted_prev_archived TINYINT(1) NOT NULL DEFAULT 0 AFTER deleted_at"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS priority TINYINT NULL DEFAULT NULL AFTER recurrence_rule"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "MODIFY COLUMN priority TINYINT NULL DEFAULT NULL"
    )
    exec_sql(
        "ALTER TABLE projects "
        "ADD COLUMN IF NOT EXISTS archived_at DATETIME NULL AFTER archived"
    )
    exec_sql(
        "ALTER TABLE projects "
        "ADD COLUMN IF NOT EXISTS deleted_at DATETIME NULL AFTER archived_at"
    )
    exec_sql(
        "ALTER TABLE projects "
        "ADD COLUMN IF NOT EXISTS deleted_prev_archived TINYINT(1) NOT NULL DEFAULT 0 AFTER deleted_at"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD INDEX IF NOT EXISTS idx_tasks_archived (archived)"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD INDEX IF NOT EXISTS idx_tasks_archived_at (archived_at)"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD INDEX IF NOT EXISTS idx_tasks_deleted_at (deleted_at)"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD INDEX IF NOT EXISTS idx_tasks_priority (priority)"
    )
    exec_sql(
        "ALTER TABLE projects "
        "ADD INDEX IF NOT EXISTS idx_projects_archived_at (archived_at)"
    )
    exec_sql(
        "ALTER TABLE projects "
        "ADD INDEX IF NOT EXISTS idx_projects_deleted_at (deleted_at)"
    )

    # Calendar sync metadata (bidireccional)
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS google_calendar_id VARCHAR(255) NULL AFTER archived_at"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS google_event_id VARCHAR(255) NULL AFTER google_calendar_id"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS google_event_etag VARCHAR(255) NULL AFTER google_event_id"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS calendar_sync_state VARCHAR(40) NOT NULL DEFAULT 'none' AFTER google_event_etag"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS calendar_sync_error TEXT NULL AFTER calendar_sync_state"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS calendar_last_synced_hash CHAR(64) NULL AFTER calendar_sync_error"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS calendar_last_synced_at DATETIME NULL AFTER calendar_last_synced_hash"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS calendar_local_changed_at DATETIME NULL AFTER calendar_last_synced_at"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS calendar_remote_updated_at DATETIME NULL AFTER calendar_local_changed_at"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS calendar_conflict_payload LONGTEXT NULL AFTER calendar_remote_updated_at"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD COLUMN IF NOT EXISTS calendar_conflict_at DATETIME NULL AFTER calendar_conflict_payload"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD INDEX IF NOT EXISTS idx_tasks_google_event_id (google_event_id)"
    )
    exec_sql(
        "ALTER TABLE tasks "
        "ADD INDEX IF NOT EXISTS idx_tasks_calendar_sync_state (calendar_sync_state)"
    )

    exec_sql(
        "CREATE TABLE IF NOT EXISTS recurring_task_runs ("
        "id INT AUTO_INCREMENT PRIMARY KEY, "
        "task_id INT NOT NULL, "
        "executed_at DATETIME NOT NULL, "
        "previous_due_date DATE NULL, "
        "next_due_date DATE NULL, "
        "created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "INDEX idx_recurring_task_runs_task (task_id), "
        "INDEX idx_recurring_task_runs_executed_at (executed_at), "
        "CONSTRAINT fk_recurring_task_runs_task "
        "FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE ON UPDATE CASCADE"
        ")"
    )

    commit()
    _schema_bootstrapped = True


def ensure_review_defaults() -> None:
    """Inserta etiquetas/carpetas necesarias para que review funcione con todos los bloques."""
    required_tags = ["NextAction","EnSeguimiento","agenda","EnEspera"]
    required_folders = ["✅ Checklists","ADTV","🔜 EstaSemanaNo"]

    for t in required_tags:
        exec_sql("INSERT IGNORE INTO tags(name) VALUES(%s)", (t,))

    for f in required_folders:
        exec_sql("INSERT IGNORE INTO folders(name, parent_id) VALUES(%s,%s)", (f, None))


def exec_sql(sql: str, params: Tuple[Any, ...] = ()) -> int:
    with db().cursor() as cur:
        cur.execute(sql, params)
        return cur.lastrowid or 0

def commit():
    db().commit()

def rollback():
    db().rollback()

from subtasks import DB as SubDB, register_subtask_routes, load_subtasks_map, load_subtask_counts

# --- Funciones auxiliares para review y otros ---
def tag_exists(name: str) -> bool:
    row = q1("SELECT id FROM tags WHERE lower(name)=lower(%s)", (name,))
    return row is not None

def folder_exists(name: str) -> bool:
    row = q1("SELECT id FROM folders WHERE lower(name)=lower(%s)", (name,))
    return row is not None

def get_folder_tree_ids(parent_id, include_self=True):
    """Obtiene recursivamente los IDs de una carpeta y sus descendientes"""
    result = set()
    if include_self:
        result.add(parent_id)
    children = q("SELECT id FROM folders WHERE parent_id=%s", (parent_id,))
    for child in children:
        result.add(child['id'])
        result.update(get_folder_tree_ids(child['id'], include_self=False))
    return result

subdb = SubDB(q=q, q1=q1, exec_sql=exec_sql, commit=commit, rollback=rollback)
register_subtask_routes(app, subdb)

# -----------------------------------------------
# ---------------- Parsing quick entry ----------------
# -----------------------------------------------

TAG_RE = re.compile(r"@([A-Za-z0-9_\-áéíóúÁÉÍÓÚñÑ]+)")
# Grupo 1 = nombre entrecomillado (con espacios), grupo 2 = nombre sin comillas (sin espacios)
PROJ_RE = re.compile(r'#(?:"([^"]+)"|([A-Za-z0-9_\-áéíóúÁÉÍÓÚñÑ][A-Za-z0-9_\-áéíóúÁÉÍÓÚñÑ]*))')
FOLDER_RE = re.compile(r'(?<![a-zA-Z])f:(?:"([^"]+)"|([A-Za-z0-9_\-áéíóúÁÉÍÓÚñÑ][A-Za-z0-9_\-áéíóúÁÉÍÓÚñÑ]*))')

def normalize_name(s: str) -> str:
    return (s or "").strip()


def normalize_tag_key(tag_name: str) -> str:
    raw = (tag_name or "").strip().lower().lstrip("@")
    normalized = unicodedata.normalize("NFD", raw)
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def is_periodic_tag_name(tag_name: str) -> bool:
    key = normalize_tag_key(tag_name)
    return key in {"periodica", "periodicas"}
    
    
DATE_TOKEN_RE = re.compile(
    r'(?<!\S)\*('
    r'\d{1,2}-\d{1,2}(?:-\d{4})?'
    r'|hoy'
    r'|mañana'
    r'|\+\d+'
    r')\b',
    re.IGNORECASE
)

DATE_BARE_RE = re.compile(
    r'(?<![#@\w])('
    r'\d{1,2}-\d{1,2}(?:-\d{4})?'
    r')\b'
)

TIME_TOKEN_RE = re.compile(r'(?<!\S)h:(\d{1,2}:\d{2})\b', re.IGNORECASE)
PRIORITY_TOKEN_RE = re.compile(r'(?<!\S)\^(alta|media|baja)\b', re.IGNORECASE)


def extract_priority_from_quick(raw_text: str) -> Tuple[Optional[int], str]:
    """
    Devuelve (priority, cleaned_text).

    Soporta tokens en texto:
    - ^alta
    - ^media
    - ^baja
    """
    s = (raw_text or "").strip()
    if not s:
        return None, s

    m = PRIORITY_TOKEN_RE.search(s)
    if not m:
        return None, s

    raw = (m.group(1) or "").strip().lower()
    prio_map = {
        "alta": 1,
        "media": 2,
        "baja": 3,
    }
    priority = prio_map.get(raw)
    cleaned = (s[:m.start()] + " " + s[m.end():]).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return priority, cleaned

def parse_due_token(token: str) -> date:
    token = (token or "").strip().lower()

    today = _today_madrid()

    if token == "hoy":
        return today

    if token == "mañana":
        return today + timedelta(days=1)

    if token.startswith("+") and token[1:].isdigit():
        return today + timedelta(days=int(token[1:]))

    m = re.fullmatch(r'(\d{1,2})-(\d{1,2})(?:-(\d{4}))?', token)
    if m:
        day = int(m.group(1))
        month = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        return datetime.strptime(f"{day:02d}-{month:02d}-{year}", "%d-%m-%Y").date()

    raise ValueError(f"Fecha inválida: {token}")


def extract_due_date_from_quick(raw_text: str) -> Tuple[Optional[date], str]:
    """
    Devuelve (due_date, cleaned_text)

    Soporta:
    - *15-03
    - *15-03-2026
    - *hoy
    - *mañana
    - *+3
    - 15-03
    - 15-03-2026

    Prioridad:
    1) fecha con *
    2) fecha 'bare' sin prefijo
    """
    s = (raw_text or "").strip()
    if not s:
        return None, s

    # 1) Buscar primero fecha con prefijo *
    m = DATE_TOKEN_RE.search(s)
    if m:
        token = m.group(1)
        due_date = parse_due_token(token)
        cleaned = (s[:m.start()] + " " + s[m.end():]).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        return due_date, cleaned

    # 2) Buscar palabras clave de fecha sin prefijo: hoy, mañana
    m = re.search(r'(?<!\w)(hoy|mañana)(?!\w)', s, re.IGNORECASE)
    if m:
        token = m.group(1).lower()
        due_date = parse_due_token(token)
        cleaned = (s[:m.start()] + " " + s[m.end():]).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        return due_date, cleaned

    # 3) Si no hay *, buscar fecha "bare" (sin prefijo)
    m = DATE_BARE_RE.search(s)
    if m:
        token = m.group(1)
        due_date = parse_due_token(token)
        cleaned = (s[:m.start()] + " " + s[m.end():]).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        return due_date, cleaned

    return None, s


def parse_due_time_token(token: str) -> time:
    m = re.fullmatch(r'(\d{1,2}):(\d{2})', (token or '').strip())
    if not m:
        raise ValueError(f"Hora inválida: {token}")

    hh = int(m.group(1))
    mm = int(m.group(2))
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        raise ValueError(f"Hora inválida: {token}")

    return time(hour=hh, minute=mm)


def extract_due_time_from_quick(raw_text: str) -> Tuple[Optional[time], str]:
    """
    Devuelve (due_time, cleaned_text)

    Soporta prefijo: h:HH:MM
    Ejemplos válidos: h:23:30, h:8:05
    """
    s = (raw_text or '').strip()
    if not s:
        return None, s

    m = TIME_TOKEN_RE.search(s)
    if not m:
        return None, s

    due_time = parse_due_time_token(m.group(1))
    cleaned = (s[:m.start()] + " " + s[m.end():]).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return due_time, cleaned


def parse_task_quick_entry(raw_title: str) -> Tuple[str, List[str], Optional[str], Optional[str]]:
    tags = TAG_RE.findall(raw_title or "")
    m = PROJ_RE.search(raw_title or "")
    project_name = (m.group(1) or m.group(2) or '').strip() if m else None
    m_folder = FOLDER_RE.search(raw_title or "")
    folder_name = (m_folder.group(1) or m_folder.group(2) or '').strip() if m_folder else None

    title = TAG_RE.sub("", raw_title)
    title = PROJ_RE.sub("", title)
    title = FOLDER_RE.sub("", title)
    title = re.sub(r"\s+", " ", title).strip()

    tags = [normalize_name(t) for t in tags if normalize_name(t)]
    if project_name:
        project_name = normalize_name(project_name)
    if folder_name:
        folder_name = normalize_name(folder_name)
    return title, tags, project_name, folder_name
    
  
    

def get_or_create_tag(tag_name: str) -> int:
    tag_name = normalize_name(tag_name)
    row = q1("SELECT id FROM tags WHERE name=%s", (tag_name,))
    if row:
        return int(row["id"])
    return exec_sql("INSERT INTO tags(name) VALUES(%s)", (tag_name,))

def find_project_by_name_active(project_name: str) -> Optional[int]:
    project_name = normalize_name(project_name)
    row = q1("SELECT id FROM projects WHERE name=%s AND archived=0", (project_name,))
    return int(row["id"]) if row else None


def find_folder_by_name(folder_name: str) -> Optional[int]:
    folder_name = normalize_name(folder_name)
    row = q1("SELECT id FROM folders WHERE name=%s", (folder_name,))
    return int(row["id"]) if row else None

def parse_tags_csv(tags_csv: str) -> List[str]:
    # Se aceptan etiquetas separadas por comas o espacios, con o sin prefijo @.
    # Ejemplos: "@NextAction, @Casa" | "@NextAction @Casa" | "next casa".
    s = (tags_csv or "").strip()
    if not s:
        return []

    parts = [p.strip() for p in re.split(r"[\s,]+", s) if p.strip()]
    tags = []

    for part in parts:
        if not part.startswith("@"):
            part = "@" + part
        m = TAG_RE.match(part)
        if m:
            tags.append(normalize_name(m.group(1)))

    return tags

# -----------------------------------------------
# ---------------- Parsing functions filters ----------------
# -----------------------------------------------

# ---------------- Filters: parsing and SQL compilation ----------------

def cfg_int(path: list[str], default: int, min_v: int = 1, max_v: int = 500) -> int:
    """
    Lee un int desde g.cfg usando una ruta tipo ["app","pagination","agenda_per_page"].
    Si falta o es inválido, devuelve default. Aplica límites min/max.
    """
    try:
        cur = getattr(g, "cfg", None) or load_config()
        for k in path:
            cur = cur.get(k, None) if isinstance(cur, dict) else None
        v = int(cur)
        if v < min_v:
            return min_v
        if v > max_v:
            return max_v
        return v
    except Exception:
        return default


def cfg_bool(path: list[str], default: bool) -> bool:
    """
    Lee un booleano desde g.cfg usando una ruta tipo ["app","behavior","flag"].
    Acepta bool, int y cadenas comunes (true/false, 1/0, yes/no, on/off).
    """
    try:
        cur = getattr(g, "cfg", None) or load_config()
        for k in path:
            cur = cur.get(k, None) if isinstance(cur, dict) else None

        if cur is None:
            return default
        if isinstance(cur, bool):
            return cur
        if isinstance(cur, (int, float)):
            return bool(cur)
        if isinstance(cur, str):
            v = cur.strip().lower()
            if v in ("1", "true", "yes", "on", "si", "sí"):
                return True
            if v in ("0", "false", "no", "off"):
                return False
        return default
    except Exception:
        return default


class FilterParseError(Exception):
    pass

def _today_madrid() -> date:
    return datetime.now(ZoneInfo("Europe/Madrid")).date()


def tokenize_filter(expr: str):
    import re

    # Convierte una expresión de filtro de usuario a tokens.
    # Soporta:
    #   - operadores lógicos: &, |, !
    #   - agrupación: ( )
    #   - etiquetas: @NextAction
    #   - comparadores de fecha: fecha<hoy, fecha <= 25-03-2026, due>=+7
    #   - prefijos como p:proyecto, f:carpeta, fr:carpeta-recursiva, fa:carpeta-anywhere,
    #     pf:proyectORfolder, prioridad:alta|media|baja
    #   - identificadores especiales: inbox, done
    tokens = []

    if not expr:
        return tokens

    # normalizar operadores escritos
    expr = re.sub(r"\band\b", "&", expr, flags=re.I)
    expr = re.sub(r"\bor\b", "|", expr, flags=re.I)

    parts = re.split(r"(\||&|\(|\)|!)", expr)

    for part in parts:
        part = part.strip()
        if not part:
            continue

        if part == "|":
            tokens.append(("OP","|"))
            continue

        if part == "&":
            tokens.append(("OP","&"))
            continue

        if part == "!":
            tokens.append(("OP","!"))
            continue

        if part == "(":
            tokens.append(("LPAREN","("))
            continue

        if part == ")":
            tokens.append(("RPAREN",")"))
            continue

        if part.startswith("@"):
            tokens.append(("TAG",part[1:]))
            continue

        # Comparadores de fecha: fecha|due + operador + referencia
        # Ejemplos válidos:
        #   fecha < hoy
        #   fecha<=25-03-2026
        #   due>=+7
        m_date = re.fullmatch(r"(?i)(fecha|due)\s*(<=|>=|=|<|>)\s*(.+)", part)
        if m_date:
            op = m_date.group(2)
            ref = m_date.group(3).strip()
            if not ref:
                raise FilterParseError("Falta referencia en comparación de fecha.")
            tokens.append(("DATECMP", f"{op} {ref}"))
            continue

        if ":" in part:
            tokens.append(("TERM",part))
            continue

        tokens.append(("IDENT",part))

    return tokens

# AST nodes
class Node: pass
class Term(Node):
    def __init__(self, kind: str, value: str):
        self.kind = kind  # TAG, IDENT, TERM, DATECMP
        self.value = value

class Not(Node):
    def __init__(self, child: Node): self.child = child

class And(Node):
    def __init__(self, left: Node, right: Node): self.left, self.right = left, right

class Or(Node):
    def __init__(self, left: Node, right: Node): self.left, self.right = left, right


def parse_filter_expression(expr: str) -> Node:
    tokens = tokenize_filter(expr)
    pos = 0

    def cur():
        return tokens[pos] if pos < len(tokens) else None

    def eat(ttype=None, tval=None):
        nonlocal pos
        tok = cur()
        if tok is None:
            return None
        if ttype and tok[0] != ttype:
            return None
        if tval and tok[1] != tval:
            return None
        pos += 1
        return tok

    # Grammar:
    # expr := or_expr
    # or_expr := and_expr ( '|' and_expr )*
    # and_expr := not_expr ( '&' not_expr )*
    # not_expr := '!' not_expr | primary
    # primary := TERM | TAG | IDENT | DATECMP | '(' expr ')'
    #
    # Esto permite expresiones complejas como:
    #   @NextAction & !done
    #   p:Inbox | f:Work
    #   (p:ProjectA & @urgent) | due<=hoy

    def parse_primary() -> Node:
        tok = cur()
        if tok is None:
            raise FilterParseError("Expresión incompleta.")
        if eat("LPAREN"):
            node = parse_or()
            if not eat("RPAREN"):
                raise FilterParseError("Falta ')'.")
            return node
        if tok[0] in ("TERM", "TAG", "IDENT", "DATECMP"):
            eat(tok[0])
            return Term(tok[0], tok[1])
        raise FilterParseError(f"Token inesperado: {tok}")

    def parse_not() -> Node:
        tok = cur()
        if tok and tok[0] == "OP" and tok[1] == "!":
            eat("OP", "!")
            return Not(parse_not())
        return parse_primary()

    def parse_and() -> Node:
        node = parse_not()
        while True:
            tok = cur()
            if tok and tok[0] == "OP" and tok[1] == "&":
                eat("OP", "&")
                node = And(node, parse_not())
            else:
                break
        return node

    def parse_or() -> Node:
        node = parse_and()
        while True:
            tok = cur()
            if tok and tok[0] == "OP" and tok[1] == "|":
                eat("OP", "|")
                node = Or(node, parse_and())
            else:
                break
        return node

    ast = parse_or()
    if pos != len(tokens):
        raise FilterParseError("Tokens sobrantes al final de la expresión.")
    return ast


def _parse_date_ref(ref: str) -> Tuple[Optional[date], bool]:
    """
    Returns (date_value, is_null_ref)
    """
    r = (ref or "").strip()
    if r.lower() == "null":
        return None, True
    if r.lower() == "hoy":
        return _today_madrid(), False
    # integer days from today: N, +N, -N
    if re.fullmatch(r"[+-]?\d+", r):
        return _today_madrid() + timedelta(days=int(r)), False
    # dd-mm-aaaa
    try:
        return datetime.strptime(r, "%d-%m-%Y").date(), False
    except ValueError:
        raise FilterParseError(f"Referencia de fecha inválida: '{ref}'. Usa hoy, NULL, N, +N, -N o dd-mm-aaaa.")



def ast_contains_done(n: Node) -> bool:
    """Detecta si el AST contiene el identificador 'done'."""
    if isinstance(n, Term):
        return n.kind == "IDENT" and n.value.lower() == "done"
    if isinstance(n, Not):
        return ast_contains_done(n.child)
    if isinstance(n, And):
        return ast_contains_done(n.left) or ast_contains_done(n.right)
    if isinstance(n, Or):
        return ast_contains_done(n.left) or ast_contains_done(n.right)
    return False


def compile_filter_to_sql(ast: Node) -> Tuple[str, List[Any]]:
    """
    Compila AST a WHERE SQL + parámetros.

    Prefijos soportados:
      @tag      => tareas con etiqueta
      p:name    => proyecto exacto
      f:name    => carpeta directa exacta
      fr:name   => folder recursivo (tareas de folder_id en el árbol)
      fa:name   => folder anywhere (folder + proyectos en el árbol)
      pf:value  => búsqueda libre en proyecto/carpeta/field
            prioridad:value / pr:value => prioridad (alta|media|baja|1|2|3|null)

    Tokens de comparadores de fechas (DATECMP) se interpretan en _parse_date_ref().
    """

    params: List[Any] = []

    def compile_node(n: Node) -> str:

        if isinstance(n, And):
            return f"({compile_node(n.left)} AND {compile_node(n.right)})"

        if isinstance(n, Or):
            return f"({compile_node(n.left)} OR {compile_node(n.right)})"

        if isinstance(n, Not):
            return f"(NOT {compile_node(n.child)})"

        if isinstance(n, Term):

            if n.kind == "IDENT":

                v = n.value.lower()

                if v == "inbox":
                    return "(t.project_id IS NULL AND t.folder_id IS NULL)"

                if v == "done":
                    return "(t.completed_at IS NOT NULL)"

                if v == "null":
                    raise FilterParseError("NULL debe usarse con p:, f: o fecha = NULL.")

                raise FilterParseError(f"Identificador desconocido: {n.value}")

            if n.kind == "TAG":

                params.append(n.value)

                return (
                    "EXISTS (SELECT 1 FROM task_tags tt "
                    "JOIN tags tg ON tg.id=tt.tag_id "
                    "WHERE tt.task_id=t.id AND tg.name=%s)"
                )

            if n.kind == "TERM":

                term = n.value
                prefix, val = term.split(":", 1)

                prefix = prefix.lower()
                val_str = (val or "").strip()

                # Permite valores entre comillas en filtros tipo p:, f:, fr:, fa:, pf:
                # Ejemplo: fa:"SomeTime"
                if len(val_str) >= 2 and (
                    (val_str[0] == '"' and val_str[-1] == '"')
                    or (val_str[0] == "'" and val_str[-1] == "'")
                ):
                    val_str = val_str[1:-1].strip()

                # -----------------------------
                # NULL handling
                # -----------------------------

                if val_str.lower() == "null":

                    if prefix == "p":
                        return "(t.project_id IS NULL)"

                    if prefix == "f":
                        return "(t.folder_id IS NULL)"

                    raise FilterParseError("NULL debe usarse con p: o f:")

                # -----------------------------
                # p: proyecto
                # -----------------------------

                if prefix == "p":

                    params.append(val_str)
                    return "(p.name=%s)"

                # -----------------------------
                # f: carpeta directa
                # -----------------------------

                if prefix == "f":

                    params.append(val_str)
                    return "(fd.name=%s)"

                # -----------------------------
                # fr: carpeta recursiva
                # Solo tareas con folder_id dentro del árbol
                # -----------------------------

                if prefix == "fr":

                    params.append(val_str)

                    return (
                        "((t.folder_id IS NOT NULL) AND t.folder_id IN ("
                        "WITH RECURSIVE subfolders AS ("
                        " SELECT id FROM folders WHERE name=%s"
                        " UNION ALL"
                        " SELECT f.id FROM folders f"
                        " JOIN subfolders sf ON f.parent_id = sf.id"
                        ") "
                        "SELECT id FROM subfolders"
                        "))"
                    )

                # -----------------------------
                # fa: folder anywhere
                # Incluye:
                #  - tareas con folder_id en la carpeta o cualquier subcarpeta
                #  - tareas de proyectos cuyo p.folder_id esté en la carpeta
                #    o cualquier subcarpeta
                # -----------------------------

                if prefix == "fa":

                    params.append(val_str)
                    params.append(val_str)

                    return (
                        "("
                        "((t.folder_id IS NOT NULL) AND t.folder_id IN ("
                        "WITH RECURSIVE subfolders AS ("
                        " SELECT id FROM folders WHERE name=%s"
                        " UNION ALL"
                        " SELECT f.id FROM folders f"
                        " JOIN subfolders sf ON f.parent_id = sf.id"
                        ") "
                        "SELECT id FROM subfolders"
                        ")) "
                        "OR "
                        "((p.folder_id IS NOT NULL) AND p.folder_id IN ("
                        "WITH RECURSIVE subfolders AS ("
                        " SELECT id FROM folders WHERE name=%s"
                        " UNION ALL"
                        " SELECT f.id FROM folders f"
                        " JOIN subfolders sf ON f.parent_id = sf.id"
                        ") "
                        "SELECT id FROM subfolders"
                        "))"
                        ")"
                    )

                # -----------------------------
                # pf: búsqueda proyecto/carpeta
                # -----------------------------

                if prefix == "pf":

                    if "/" in val_str:

                        folder_name, project_name = val_str.split("/", 1)

                        folder_name = folder_name.strip()
                        project_name = project_name.strip()

                        if not folder_name or not project_name:
                            raise FilterParseError('pf:"Carpeta/Proyecto" requiere ambos nombres.')

                        params.append(project_name)
                        params.append(folder_name)

                        return "(p.name=%s AND pf.name=%s)"

                    kw = f"%{val_str.lower()}%"

                    params.extend([kw, kw, kw])

                    return (
                        "("
                        "LOWER(p.name) LIKE %s "
                        "OR LOWER(fd.name) LIKE %s "
                        "OR LOWER(pf.name) LIKE %s"
                        ")"
                    )

                # -----------------------------
                # prioridad: / pr:
                # -----------------------------

                if prefix in ("prioridad", "priority", "prio", "pr"):
                    v = val_str.lower()

                    if v in ("1", "alta", "high"):
                        params.append(1)
                        return "(t.priority=%s)"

                    if v in ("2", "media", "medium"):
                        params.append(2)
                        return "(t.priority=%s)"

                    if v in ("3", "baja", "low"):
                        params.append(3)
                        return "(t.priority=%s)"

                    if v in ("null", "none", "sin", "ninguna"):
                        return "(t.priority IS NULL)"

                    raise FilterParseError(
                        "Prioridad inválida. Usa prioridad:alta|media|baja|1|2|3|null."
                    )

                raise FilterParseError(f"Prefijo desconocido: {prefix}:")

            if n.kind == "DATECMP":

                op, ref = n.value.split(" ", 1)

                d, is_null = _parse_date_ref(ref)

                if is_null:

                    if op != "=":
                        return "(1=0)"

                    return "(t.due_date IS NULL)"

                params.append(d)

                return f"(t.due_date IS NOT NULL AND t.due_date {op} %s)"

            raise FilterParseError("Término no soportado.")

        raise FilterParseError("AST inválido.")

    where = compile_node(ast)

    return where, params

# -----------------------------------------------    
# ---------------- Recurrence RRULE basic ----------------
# -----------------------------------------------

@dataclass
class RRule:
    freq: str
    interval: int = 1
    byday: Optional[List[str]] = None
    bymonthday: Optional[int] = None

def parse_rrule(rrule: str) -> Optional[RRule]:
    if not rrule:
        return None
    parts: Dict[str, str] = {}
    for item in rrule.split(";"):
        if "=" in item:
            k, v = item.split("=", 1)
            parts[k.strip().upper()] = v.strip().upper()
    freq = parts.get("FREQ")
    if not freq:
        return None
    interval = int(parts.get("INTERVAL", "1") or "1")
    byday = [d.strip() for d in parts.get("BYDAY", "").split(",") if d.strip()] or None
    bymonthday = None
    if parts.get("BYMONTHDAY"):
        try:
            bymonthday = int(parts["BYMONTHDAY"])
        except ValueError:
            bymonthday = None
    return RRule(freq=freq, interval=interval, byday=byday, bymonthday=bymonthday)

WEEKDAY_MAP = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}

def next_due_date(current_due: date, rule: RRule) -> date:
    if rule.freq == "DAILY":
        return current_due + timedelta(days=rule.interval)

    if rule.freq == "WEEKLY":
        if rule.byday:
            targets = sorted(WEEKDAY_MAP[d] for d in rule.byday if d in WEEKDAY_MAP)
            if targets:
                for delta in range(1, 15):
                    cand = current_due + timedelta(days=delta)
                    if cand.weekday() in targets:
                        return cand
        return current_due + timedelta(weeks=rule.interval)

    if rule.freq == "MONTHLY":
        y, m = current_due.year, current_due.month
        m += rule.interval
        while m > 12:
            y += 1
            m -= 12
        day = rule.bymonthday if rule.bymonthday else min(current_due.day, 28)
        for d in range(day, 0, -1):
            try:
                return date(y, m, d)
            except ValueError:
                continue
        return date(y, m, 1)

    if rule.freq == "YEARLY":
      y = current_due.year + rule.interval
      m = current_due.month
      d = current_due.day

      # Manejo seguro de 29 Feb, etc.
      while True:
        try:
            return date(y, m, d)
        except ValueError:
            d -= 1
            if d < 1:
                return date(y, m, 1)

    return current_due + timedelta(weeks=1)


def next_due_date_after_today(current_due: date, rule: RRule, today_d: date) -> date:
    """Return the first recurrence date on or after today."""
    due = next_due_date(current_due, rule)
    guard = 0
    while due < today_d and guard < 512:
        nxt = next_due_date(due, rule)
        if nxt <= due:
            break
        due = nxt
        guard += 1
    return due

# -----------------------------------------------
# ---------------- Sidebar folder tree ----------------
# -----------------------------------------------

def load_folder_tree(include_archived: bool = False) -> List[Dict[str, Any]]:
    folders = q("SELECT id, parent_id, name FROM folders ORDER BY name")
    projects = q(
        "SELECT id, folder_id, name, archived FROM projects "
        + ("" if include_archived else "WHERE archived=0 ")
        + "ORDER BY name"
    )

    folder_map: Dict[int, Dict[str, Any]] = {}
    roots: List[Dict[str, Any]] = []

    for f in folders:
        folder_map[int(f["id"])] = {"id": int(f["id"]), "name": f["name"], "children": [], "projects": []}

    for f in folders:
        fid = int(f["id"])
        pid = f["parent_id"]
        if pid is None:
            roots.append(folder_map[fid])
        else:
            parent = folder_map.get(int(pid))
            if parent:
                parent["children"].append(folder_map[fid])
            else:
                roots.append(folder_map[fid])

    orphan_projects: List[Dict[str, Any]] = []
    for p in projects:
        item = {"id": int(p["id"]), "name": p["name"], "archived": int(p["archived"]) == 1}
        if p["folder_id"] is None:
            orphan_projects.append(item)
        else:
            node = folder_map.get(int(p["folder_id"]))
            if node is not None:
                node["projects"].append(item)
            else:
                orphan_projects.append(item)

    if orphan_projects:
        roots.append({"id": None, "name": "Proyectos (sin carpeta)", "children": [], "projects": orphan_projects})

    return roots


def build_folder_breadcrumb(folder_id: Optional[int], include_self: bool = True) -> List[Dict[str, Any]]:
    """Devuelve la ruta de carpetas desde raíz hasta folder_id."""
    if not folder_id:
        return []

    rows = q("SELECT id, parent_id, name FROM folders")
    folder_map: Dict[int, Dict[str, Any]] = {}
    for r in rows:
        folder_map[int(r["id"])] = {
            "id": int(r["id"]),
            "parent_id": int(r["parent_id"]) if r.get("parent_id") is not None else None,
            "name": r["name"],
        }

    current_id = int(folder_id)
    visited: set[int] = set()
    chain: List[Dict[str, Any]] = []

    while current_id in folder_map and current_id not in visited:
        visited.add(current_id)
        node = folder_map[current_id]
        chain.append({"id": node["id"], "name": node["name"]})
        parent_id = node["parent_id"]
        if parent_id is None:
            break
        current_id = parent_id

    chain.reverse()
    if not include_self and chain:
        return chain[:-1]
    return chain


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
                "SELECT t.id, t.title, t.notes, t.due_date, t.completed_at, t.recurrence_rule, t.archived, t.archived_at, "
                "p.name AS project_name, p.id AS project_id, "
                "fd.id AS folder_id, fd.name AS folder_name "
                "FROM tasks t "
                "LEFT JOIN projects p ON p.id=t.project_id "
                "LEFT JOIN folders fd ON fd.id = COALESCE(t.folder_id, p.folder_id) "
                "WHERE t.deleted_at IS NULL "
            )
            _task_order = (
                "ORDER BY t.archived ASC, (t.completed_at IS NOT NULL) ASC, "
                "(t.due_date IS NULL) ASC, t.due_date ASC, t.id DESC "
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
                "SELECT tg.id, tg.name, tg.type, "
                "(SELECT COUNT(*) FROM task_tags tt JOIN tasks t ON t.id=tt.task_id WHERE tt.tag_id=tg.id AND t.archived=0 AND t.deleted_at IS NULL) AS task_count "
                "FROM tags tg "
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
                "  AND (LOWER(t.title) LIKE %s OR LOWER(COALESCE(t.notes, '')) LIKE %s)"
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
                "    AND (LOWER(t.title) LIKE %s OR LOWER(COALESCE(t.notes, '')) LIKE %s) "
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
                    "  AND (LOWER(t.title) LIKE %s OR LOWER(COALESCE(t.notes, '')) LIKE %s)"
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
                    "    AND (LOWER(t.title) LIKE %s OR LOWER(COALESCE(t.notes, '')) LIKE %s) "
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


# -----------------------------------------------
# ---------------- Tags map (id + name) ----------------
# -----------------------------------------------

def load_tags_map(task_ids: List[int]) -> Dict[int, List[Dict[str, Any]]]:
    if not task_ids:
        return {}
    placeholders = ",".join(["%s"] * len(task_ids))
    rows = q(
        f"SELECT tt.task_id, tg.id AS tag_id, tg.name "
        f"FROM task_tags tt "
        f"JOIN tags tg ON tg.id=tt.tag_id "
        f"WHERE tt.task_id IN ({placeholders}) "
        f"ORDER BY tg.name",
        tuple(task_ids),
    )
    out: Dict[int, List[Dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(int(r["task_id"]), []).append({"id": int(r["tag_id"]), "name": r["name"]})
    return out

# -----------------------------------------------
# ---------------- Common template context ----------------
# -----------------------------------------------

@app.context_processor
def inject_common():
    cfg = load_config()
    return {
        "APP_TITLE": cfg.get("app", {}).get("title", "GTD App"),
        "folder_tree": load_folder_tree(include_archived=False),
        "all_folders": q("SELECT id, name FROM folders ORDER BY name"),
        "all_projects": q("SELECT id, name FROM projects WHERE archived=0 AND deleted_at IS NULL ORDER BY name"),
        "TAG_SEARCH_URL": url_for("api_tags_search"),
        "PROJECT_SEARCH_URL": url_for("api_projects_search"),
        "FOLDER_SEARCH_URL": url_for("api_folders_search"),
        "SCRIPT_ROOT": request.script_root or "",
    }

@app.context_processor
def inject_sidebar_counts():
    try:
        today_d = _today_madrid()
        sunday_d = today_d + timedelta(days=(6 - today_d.weekday()))
        
        
        next_tag = q1("SELECT id FROM tags WHERE name=%s", ("NextAction",))

        if next_tag:
            next_count = int((q1(
                "SELECT COUNT(*) AS c "
                "FROM tasks t "
                "JOIN task_tags tt ON tt.task_id=t.id "
                "LEFT JOIN projects p ON p.id=t.project_id "
                "WHERE t.completed_at IS NULL "
                "AND tt.tag_id=%s "
                "AND (t.project_id IS NULL OR p.archived = 0)",
                (next_tag["id"],)
            ) or {}).get("c", 0))
        else:
            next_count = -1

        counts = {
            "next": next_count,
            
            "inbox": int((q1(
                "SELECT COUNT(*) AS c "
                "FROM tasks "
                "WHERE completed_at IS NULL AND project_id IS NULL AND folder_id IS NULL"
            ) or {}).get("c", 0)),

            "today": int((q1(
                "SELECT COUNT(*) AS c "
                "FROM tasks t "
                "LEFT JOIN projects p ON p.id=t.project_id "
                "WHERE t.completed_at IS NULL AND t.due_date=%s "
                "AND (t.project_id IS NULL OR p.archived = 0)",
                (today_d,)
            ) or {}).get("c", 0)),

            "week": int((q1(
                "SELECT COUNT(*) AS c "
                "FROM tasks t "
                "LEFT JOIN projects p ON p.id=t.project_id "
                "WHERE t.completed_at IS NULL "
                "AND t.due_date IS NOT NULL "
                "AND t.due_date >= %s "
                "AND t.due_date <= %s "
                "AND (t.project_id IS NULL OR p.archived = 0)",
                (today_d, sunday_d)
            ) or {}).get("c", 0)),

            "next": next_count,

            "agenda": int((q1(
                "SELECT COUNT(*) AS c "
                "FROM tasks t "
                "LEFT JOIN projects p ON p.id=t.project_id "
                "WHERE t.completed_at IS NULL AND t.due_date IS NOT NULL "
                "AND (t.project_id IS NULL OR p.archived = 0)"
            ) or {}).get("c", 0)),

            "projects": int((q1(
                "SELECT COUNT(*) AS c "
                "FROM projects "
                "WHERE archived=0"
            ) or {}).get("c", 0)),

            "tags": int((q1(
                "SELECT COUNT(*) AS c "
                "FROM tags"
            ) or {}).get("c", 0)),

            "filters": int((q1(
                "SELECT COUNT(*) AS c "
                "FROM filters"
            ) or {}).get("c", 0)),
        }

    except Exception:
        counts = {
            "next": 0,
            "inbox": 0,
            "today": 0,
            "week": 0,
            "agenda": 0,
            "projects": 0,
            "tags": 0,
            "filters": 0,
        }

    return {"sidebar_counts": counts}
    
# -----------------------------------------------    
# ---------------- Routes: views ----------------
# -----------------------------------------------

@app.route("/")
def home():       
    
    inbox = q(
        "SELECT t.id, t.title, t.notes, t.due_date, t.due_time, t.completed_at, t.recurrence_rule "
        "FROM tasks t "
        "WHERE t.project_id IS NULL AND t.folder_id IS NULL AND t.archived=0 AND t.deleted_at IS NULL "
        "ORDER BY (t.due_date IS NULL), t.due_date ASC, t.id DESC "
        "LIMIT 200"
    )

    # proyectos sin carpeta
    orphan_projects = q(
        "SELECT id, name, archived, archived_at "
        "FROM projects "
        "WHERE folder_id IS NULL AND deleted_at IS NULL "
        "ORDER BY archived ASC, name ASC"
    )

    inbox_ids = [r["id"] for r in inbox]
    tags_map = load_tags_map(inbox_ids) if inbox_ids else {}
    sub_counts = load_subtask_counts(subdb, inbox_ids)
    sub_map = load_subtasks_map(subdb, inbox_ids)

    return render_template(
        "home.html",
        inbox=inbox,
        tags_map=tags_map,
        sub_counts=sub_counts,
        sub_map=sub_map,
        orphan_projects=orphan_projects
    )

@app.route("/agenda")
def agenda_legacy():
    return redirect(url_for("proximo"), code=301)


@app.route("/proximo")
def proximo():

    per_page = cfg_int(["app", "pagination", "agenda_per_page"], default=25, min_v=5, max_v=500)

    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    page = max(page, 1)
    offset = (page - 1) * per_page

    # Total de tareas con fecha (si tu agenda incluye solo tareas con due_date)
    total_row = q1(
        "SELECT COUNT(*) AS c FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE t.due_date IS NOT NULL AND t.completed_at IS NULL "
        "AND t.deleted_at IS NULL "
        "AND (t.project_id IS NULL OR p.archived = 0)"
    )
    total = int(total_row["c"]) if total_row else 0
    pages = max(1, (total + per_page - 1) // per_page)

    rows = q(
        "SELECT t.id, t.title, t.notes, t.due_date, t.due_time, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.id AS folder_id, fd.name AS folder_name "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "WHERE t.due_date IS NOT NULL "
        "AND t.completed_at IS NULL "
        "AND t.deleted_at IS NULL "
        "AND (t.project_id IS NULL OR p.archived = 0) "
        "ORDER BY (t.completed_at IS NOT NULL) ASC, t.due_date ASC, (t.due_time IS NULL) ASC, t.due_time ASC, t.id DESC "
        "LIMIT %s OFFSET %s",
        (per_page, offset),
    )
    
    
    task_ids = [r["id"] for r in rows]
    tags_map = load_tags_map(task_ids) if task_ids else {}
    sub_counts = load_subtask_counts(subdb, task_ids)
    sub_map = load_subtasks_map(subdb, task_ids)

    today_d = _today_madrid()

    return render_template(
        "proximo.html",
        rows=rows,
        tags_map=tags_map,
        sub_counts=sub_counts,
        sub_map=sub_map,
        today_d=today_d,
        page=page,
        pages=pages,
        total=total,
        per_page=per_page,
    )

@app.route("/today")
def today():
    today_d = _today_madrid()

    recurring_due_join = (
        "LEFT JOIN ("
        "SELECT task_id, MAX(next_due_date) AS last_next_due_date "
        "FROM recurring_task_runs "
        "GROUP BY task_id"
        ") rr ON rr.task_id=t.id "
    )
    effective_due_expr = (
        "CASE "
        "WHEN t.recurrence_rule IS NOT NULL AND TRIM(t.recurrence_rule) <> '' "
        "AND rr.last_next_due_date IS NOT NULL "
        "AND (t.due_date IS NULL OR rr.last_next_due_date > t.due_date) "
        "THEN rr.last_next_due_date "
        "ELSE t.due_date "
        "END"
    )

    pending_rows = q(
        f"SELECT t.id, t.title, t.notes, {effective_due_expr} AS due_date, t.due_time, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.id AS folder_id, fd.name AS folder_name "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        f"{recurring_due_join}"
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        f"WHERE {effective_due_expr}=%s AND t.completed_at IS NULL "
        "AND t.deleted_at IS NULL "
        "AND (t.project_id IS NULL OR p.archived = 0) "
        "ORDER BY t.id DESC",
        (today_d,)
    )

    overdue_rows = q(
        f"SELECT t.id, t.title, t.notes, {effective_due_expr} AS due_date, t.due_time, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.id AS folder_id, fd.name AS folder_name "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        f"{recurring_due_join}"
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        f"WHERE {effective_due_expr} < %s AND t.completed_at IS NULL "
        "AND t.deleted_at IS NULL "
        "AND (t.project_id IS NULL OR p.archived = 0) "
        f"ORDER BY {effective_due_expr} ASC, t.id DESC",
        (today_d,)
    )

    done_rows = q(
        "("
        "SELECT t.id, t.title, t.notes, t.due_date, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.id AS folder_id, fd.name AS folder_name "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "WHERE t.completed_at IS NOT NULL AND DATE(t.completed_at)=%s "
        "AND t.deleted_at IS NULL "
        "UNION ALL "
        "SELECT t.id, t.title, t.notes, t.due_date, t.last_completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.id AS folder_id, fd.name AS folder_name "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "WHERE t.last_completed_at IS NOT NULL AND DATE(t.last_completed_at)=%s "
        "AND t.recurrence_rule IS NOT NULL AND TRIM(t.recurrence_rule) <> '' "
        "AND t.deleted_at IS NULL "
        ") "
        "ORDER BY completed_at DESC, id DESC",
        (today_d, today_d)
    )

    all_ids = [r["id"] for r in pending_rows] + [r["id"] for r in overdue_rows] + [r["id"] for r in done_rows]
    tags_map = load_tags_map(all_ids) if all_ids else {}
    sub_counts = load_subtask_counts(subdb, all_ids)
    sub_map = load_subtasks_map(subdb, all_ids)

    return render_template(
        "today.html",
        pending_rows=pending_rows,
        overdue_rows=overdue_rows,
        done_rows=done_rows,
        tags_map=tags_map,
        sub_counts=sub_counts,
        sub_map=sub_map,
        today=today_d,
    )


@app.route("/week")
def week():

    from datetime import timedelta

    today_d = _today_madrid()

    # lunes de la semana actual
    monday_d = today_d - timedelta(days=today_d.weekday())

    # domingo de la semana actual
    sunday_d = monday_d + timedelta(days=6)

    recurring_due_join = (
        "LEFT JOIN ("
        "SELECT task_id, MAX(next_due_date) AS last_next_due_date "
        "FROM recurring_task_runs "
        "GROUP BY task_id"
        ") rr ON rr.task_id=t.id "
    )
    effective_due_expr = (
        "CASE "
        "WHEN t.recurrence_rule IS NOT NULL AND TRIM(t.recurrence_rule) <> '' "
        "AND rr.last_next_due_date IS NOT NULL "
        "AND (t.due_date IS NULL OR rr.last_next_due_date > t.due_date) "
        "THEN rr.last_next_due_date "
        "ELSE t.due_date "
        "END"
    )

    overdue_rows = q(
        f"SELECT t.id, t.title, t.notes, {effective_due_expr} AS due_date, t.due_time, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        f"{recurring_due_join}"
        "LEFT JOIN folders fd ON fd.id = COALESCE(t.folder_id, p.folder_id) "
        f"WHERE {effective_due_expr} IS NOT NULL "
        f"AND {effective_due_expr} < %s "
        "AND t.completed_at IS NULL "
        "AND t.deleted_at IS NULL "
        "AND (t.project_id IS NULL OR p.archived = 0) "
        f"ORDER BY {effective_due_expr} ASC, t.id DESC",
        (monday_d,)
    )

    rows = q(
        "SELECT * FROM ("
        f"SELECT t.id, t.title, t.notes, {effective_due_expr} AS due_date, t.due_time, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        f"{recurring_due_join}"
        "LEFT JOIN folders fd ON fd.id = COALESCE(t.folder_id, p.folder_id) "
        f"WHERE {effective_due_expr} IS NOT NULL "
        f"AND {effective_due_expr} >= %s "
        f"AND {effective_due_expr} <= %s "
        "AND t.completed_at IS NULL "
        "AND t.deleted_at IS NULL "
        "AND (t.project_id IS NULL OR p.archived = 0) "
        "UNION ALL "
        f"SELECT t.id, t.title, t.notes, {effective_due_expr} AS due_date, t.due_time, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        f"{recurring_due_join}"
        "LEFT JOIN folders fd ON fd.id = COALESCE(t.folder_id, p.folder_id) "
        f"WHERE {effective_due_expr} IS NOT NULL "
        f"AND {effective_due_expr} >= %s "
        f"AND {effective_due_expr} <= %s "
        "AND t.completed_at IS NOT NULL "
        "AND t.deleted_at IS NULL "
        ") AS week_rows "
        "ORDER BY (completed_at IS NOT NULL) ASC, due_date ASC, id DESC",
        (monday_d, sunday_d, monday_d, sunday_d)
    )

    task_ids = [r["id"] for r in overdue_rows] + [r["id"] for r in rows]
    tags_map = load_tags_map(task_ids) if task_ids else {}
    sub_counts = load_subtask_counts(subdb, task_ids)
    sub_map = load_subtasks_map(subdb, task_ids)

    return render_template(
        "week.html",
        overdue_rows=overdue_rows,
        rows=rows,
        tags_map=tags_map,
        today=today_d,
        sunday=sunday_d,
        monday=monday_d,
    )


def _parse_yyyy_mm_dd(value: Optional[str], default: date) -> date:
    if not value:
        return default
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except Exception:
        return default


def _build_month_weeks(year: int, month: int) -> List[List[Optional[date]]]:
    cal = calendar.Calendar(firstweekday=0)
    weeks: List[List[Optional[date]]] = []
    for week in cal.monthdatescalendar(year, month):
        row: List[Optional[date]] = []
        for d in week:
            row.append(d if d.month == month else None)
        weeks.append(row)
    return weeks


@app.route("/calendar", endpoint="calendar")
def calendar_view():
    view = (request.args.get("view") or "month").strip().lower()
    if view not in ("day", "week", "next7", "month"):
        view = "month"

    today_date = _today_madrid()
    selected_date = _parse_yyyy_mm_dd(request.args.get("date"), today_date)

    if view == "month":
        start_date = selected_date.replace(day=1)
        last_day = calendar.monthrange(selected_date.year, selected_date.month)[1]
        end_date = selected_date.replace(day=last_day)
        prev_date = (start_date - timedelta(days=1)).replace(day=1)
        next_month_first = start_date + timedelta(days=last_day)
        next_date = next_month_first.replace(day=1)
        week_days = None
    elif view == "week":
        start_date = selected_date - timedelta(days=selected_date.weekday())
        end_date = start_date + timedelta(days=6)
        prev_date = start_date - timedelta(days=7)
        next_date = start_date + timedelta(days=7)
        week_days = [start_date + timedelta(days=i) for i in range(7)]
    elif view == "next7":
        start_date = selected_date
        end_date = selected_date + timedelta(days=7)
        prev_date = selected_date - timedelta(days=7)
        next_date = selected_date + timedelta(days=7)
        week_days = None
    else:
        start_date = selected_date
        end_date = selected_date
        prev_date = selected_date - timedelta(days=1)
        next_date = selected_date + timedelta(days=1)
        week_days = None

    show_completed = str(request.args.get("show_completed", "0")).lower() in ("1", "true", "on", "yes")

    week_list_rows = []

    # Pendientes por due_date dentro de rango
    pending_rows = q(
        "SELECT t.id, t.title, t.notes, t.due_date, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.id AS folder_id, fd.name AS folder_name "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=COALESCE(t.folder_id, p.folder_id) "
        "WHERE t.due_date IS NOT NULL "
        "AND t.due_date >= %s "
        "AND t.due_date <= %s "
        "AND t.completed_at IS NULL "
        "AND t.deleted_at IS NULL "
        "AND (t.project_id IS NULL OR p.archived = 0) "
        "ORDER BY t.due_date ASC, t.id DESC",
        (start_date, end_date),
    )

    # Completadas según completed_at (independiente due_date)
    completed_rows = q(
        "SELECT t.id, t.title, t.notes, t.due_date, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.id AS folder_id, fd.name AS folder_name "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=COALESCE(t.folder_id, p.folder_id) "
        "WHERE t.completed_at IS NOT NULL "
        "AND t.deleted_at IS NULL "
        "AND DATE(t.completed_at) >= %s "
        "AND DATE(t.completed_at) <= %s "
        "ORDER BY t.completed_at DESC, t.id DESC",
        (start_date, end_date),
    )

    pending_counts = {}
    for r in pending_rows:
        d = r.get("due_date")
        if d:
            key = d.isoformat()
            pending_counts[key] = pending_counts.get(key, 0) + 1

    completed_counts = {}
    for r in completed_rows:
        d = r.get("completed_at")
        if d:
            key = d.date().isoformat()
            completed_counts[key] = completed_counts.get(key, 0) + 1

    selected_day_pending = [t for t in pending_rows if t.get("due_date") == selected_date] if view == "day" else []
    selected_day_completed = [t for t in completed_rows if t.get("completed_at") and t.get("completed_at").date() == selected_date] if view == "day" else []
    next7_rows = pending_rows if view == "next7" else []

    selected_day_tasks = list(selected_day_pending)
    if show_completed:
        pending_ids = {t["id"] for t in selected_day_pending}
        for t in selected_day_completed:
            if t["id"] not in pending_ids:
                selected_day_tasks.append(t)

    tags_map = load_tags_map([t["id"] for t in selected_day_tasks]) if selected_day_tasks else {}
    has_done = len(selected_day_completed)

    if view == "next7":
        next7_task_ids = [t["id"] for t in next7_rows]
        tags_map = load_tags_map(next7_task_ids) if next7_task_ids else {}

    if view == "week":
        if show_completed:
            week_list_rows = q(
                "SELECT * FROM ("
                "SELECT t.id, t.title, t.notes, t.due_date, t.completed_at, t.recurrence_rule, t.priority, "
                "p.name AS project_name, p.id AS project_id, "
                "fd.name AS folder_name, fd.id AS folder_id "
                "FROM tasks t "
                "LEFT JOIN projects p ON p.id=t.project_id "
                "LEFT JOIN folders fd ON fd.id = COALESCE(t.folder_id, p.folder_id) "
                "WHERE t.due_date IS NOT NULL "
                "AND t.due_date >= %s "
                "AND t.due_date <= %s "
                "AND t.completed_at IS NULL "
                "AND t.deleted_at IS NULL "
                "AND (t.project_id IS NULL OR p.archived = 0) "
                "UNION ALL "
                "SELECT t.id, t.title, t.notes, COALESCE(t.due_date, DATE(t.completed_at)) AS due_date, "
                "t.completed_at, t.recurrence_rule, t.priority, "
                "p.name AS project_name, p.id AS project_id, "
                "fd.name AS folder_name, fd.id AS folder_id "
                "FROM tasks t "
                "LEFT JOIN projects p ON p.id=t.project_id "
                "LEFT JOIN folders fd ON fd.id = COALESCE(t.folder_id, p.folder_id) "
                "WHERE t.completed_at IS NOT NULL "
                "AND DATE(t.completed_at) >= %s "
                "AND DATE(t.completed_at) <= %s "
                "AND t.deleted_at IS NULL "
                ") AS week_rows "
                "ORDER BY (completed_at IS NOT NULL) ASC, due_date ASC, id DESC",
                (start_date, end_date, start_date, end_date),
            )
        else:
            week_list_rows = q(
                "SELECT t.id, t.title, t.notes, t.due_date, t.completed_at, t.recurrence_rule, t.priority, "
                "p.name AS project_name, p.id AS project_id, "
                "fd.name AS folder_name, fd.id AS folder_id "
                "FROM tasks t "
                "LEFT JOIN projects p ON p.id=t.project_id "
                "LEFT JOIN folders fd ON fd.id = COALESCE(t.folder_id, p.folder_id) "
                "WHERE t.due_date IS NOT NULL "
                "AND t.due_date >= %s "
                "AND t.due_date <= %s "
                "AND t.completed_at IS NULL "
                "AND t.deleted_at IS NULL "
                "AND (t.project_id IS NULL OR p.archived = 0) "
                "ORDER BY t.due_date ASC, t.id DESC",
                (start_date, end_date),
            )
        week_task_ids = [t["id"] for t in week_list_rows]
        tags_map = load_tags_map(week_task_ids) if week_task_ids else {}

    month_weeks = _build_month_weeks(selected_date.year, selected_date.month)

    return render_template(
        "calendar.html",
        view=view,
        selected_date=selected_date,
        today_date=today_date,
        start_date=start_date,
        end_date=end_date,
        prev_date=prev_date,
        next_date=next_date,
        month_weeks=month_weeks,
        week_days=week_days,
        pending_counts=pending_counts,
        completed_counts=completed_counts,
        show_completed=show_completed,
        selected_day_tasks=selected_day_tasks,
        selected_day_pending=selected_day_pending,
        selected_day_completed=selected_day_completed,
        next7_rows=next7_rows,
        week_list_rows=week_list_rows,
        has_done=has_done,
        tags_map=tags_map,
    )


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
        excluded_root_names = {"rutinas", "agenda", "sometime", "adtv", "seguimiento"}
        folder_rows = q("SELECT id, parent_id, name FROM folders")
        children_by_parent: Dict[Optional[int], List[int]] = {}
        excluded_folder_ids: set[int] = set()

        for fr in folder_rows:
            parent_id = fr.get("parent_id")
            children_by_parent.setdefault(parent_id, []).append(int(fr["id"]))
            if normalize_tag_key(fr.get("name") or "") in excluded_root_names:
                excluded_folder_ids.add(int(fr["id"]))

        stack = list(excluded_folder_ids)
        while stack:
            fid = stack.pop()
            for ch_id in children_by_parent.get(fid, []):
                if ch_id not in excluded_folder_ids:
                    excluded_folder_ids.add(ch_id)
                    stack.append(ch_id)

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
    project = q1("SELECT id, name, description, archived, archived_at, folder_id, auto_promote_nextaction FROM projects WHERE id=%s", (project_id,))
    if not project:
        abort(404)

    folder_breadcrumb = build_folder_breadcrumb(project.get("folder_id"), include_self=True)

    active_tasks = q(
        "SELECT id, title, notes, due_date, due_time, completed_at, recurrence_rule, priority, sort_order "
        "FROM tasks "
        "WHERE project_id=%s AND completed_at IS NULL AND archived=0 AND deleted_at IS NULL "
        "ORDER BY COALESCE(sort_order, 2147483647) ASC, id ASC",
        (project_id,),
    )

    done_tasks = q(
        "SELECT id, title, notes, due_date, due_time, completed_at, recurrence_rule, priority "
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
    )


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


import uuid
from datetime import datetime

def create_api_token(user_id: int, device_name: str) -> str:
    """Crea un token API único y lo guarda en la tabla api_tokens."""
    token = str(uuid.uuid4())
    now = datetime.now()
    exec_sql(
        """
        INSERT INTO api_tokens (user_id, device_name, token, created_at, active)
        VALUES (%s, %s, %s, %s, 1)
        """,
        (user_id, device_name, token, now)
    )
    commit()
    return token

def revoke_api_token(token: str) -> None:
    """Revoca (desactiva) un token API."""
    exec_sql(
        "UPDATE api_tokens SET active=0 WHERE token=%s",
        (token,)
    )
    commit()

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



@app.route("/tags/<int:tag_id>/edit", methods=["GET", "POST"])
def tag_edit(tag_id: int):
    tag = q1("SELECT id, name, type FROM tags WHERE id=%s", (tag_id,))
    if not tag:
        abort(404)

    type_rows = q(
        "SELECT DISTINCT tg.type "
        "FROM tags tg "
        "WHERE tg.type IS NOT NULL AND TRIM(tg.type) <> '' "
        "ORDER BY tg.type ASC"
    )
    existing_tag_types = [r["type"] for r in type_rows]

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

        try:
            exec_sql("UPDATE tags SET name=%s, type=%s WHERE id=%s", (name, tag_type, tag_id))
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
            where_parts.append("(tg.type IS NULL OR TRIM(tg.type) = '')")
        else:
            where_parts.append("LOWER(COALESCE(tg.type, '')) = %s")
            params.append(type_filter.lower())

    where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

    total_row = q1(f"SELECT COUNT(*) AS c FROM tags tg {where}", tuple(params))
    total = int(total_row["c"]) if total_row else 0
    pages = max(1, (total + per_page - 1) // per_page)

    if page > pages:
        page = pages
        offset = (page - 1) * per_page

    # (opcional pero útil) contador de tareas por etiqueta
    rows = q(
        "SELECT tg.id, tg.name, tg.type, COUNT(t.id) AS task_count "
        "FROM tags tg "
        "LEFT JOIN task_tags tt ON tt.tag_id=tg.id "
        "LEFT JOIN tasks t ON t.id=tt.task_id AND t.archived=0 AND t.deleted_at IS NULL "
        f"{where} "
        "GROUP BY tg.id, tg.name, tg.type "
        "ORDER BY tg.name ASC, (tg.type IS NULL) ASC, tg.type ASC  "
        "LIMIT %s OFFSET %s",
        tuple(params + [per_page, offset]),
    )

    type_rows = q(
        "SELECT DISTINCT tg.type "
        "FROM tags tg "
        "WHERE tg.type IS NOT NULL AND TRIM(tg.type) <> '' "
        "ORDER BY tg.name asc, tg.type"
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

    # total de tareas con esa etiqueta
    total_row = q1(
        "SELECT COUNT(*) AS c "
        "FROM task_tags tt "
        "JOIN tasks t ON t.id=tt.task_id "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE tt.tag_id=%s "
        "AND t.archived=0 "
        "AND t.deleted_at IS NULL "
        "AND (t.project_id IS NULL OR p.archived=0)",
        (tag_id,),
    )
    total = int(total_row["c"]) if total_row else 0
    pages = max(1, (total + per_page - 1) // per_page)

    # tareas (con proyecto para mostrarlo/enlazarlo)
    rows = q(
        "SELECT t.id, t.title, t.notes, t.due_date, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.id AS folder_id, fd.name AS folder_name "
        "FROM task_tags tt "
        "JOIN tasks t ON t.id=tt.task_id "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "WHERE tt.tag_id=%s "
        "AND t.archived=0 "
        "AND t.deleted_at IS NULL "
        "AND (t.project_id IS NULL OR p.archived=0) "
        "ORDER BY (t.completed_at IS NOT NULL) ASC, (t.due_date IS NULL) ASC, t.due_date ASC, t.id DESC "
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
    )


@app.route("/periodic/history")
def periodic_history():
    try:
        tag_id = int(request.args.get("tag_id", "0"))
    except ValueError:
        tag_id = 0

    tag = None
    if tag_id > 0:
        tag = q1("SELECT id, name FROM tags WHERE id=%s", (tag_id,))

    if not tag:
        # Acceso directo sin id: usa la primera etiqueta periódica disponible.
        tag = q1(
            "SELECT id, name FROM tags "
            "WHERE LOWER(name) IN (%s, %s, %s, %s) "
            "ORDER BY id ASC "
            "LIMIT 1",
            ("periodica", "periódica", "periodicas", "periódicas"),
        )

    if not tag:
        abort(404)

    tag_id = int(tag["id"])

    per_page = cfg_int(["app", "pagination", "tag_detail_per_page"], default=25, min_v=5, max_v=500)
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    page = max(page, 1)
    offset = (page - 1) * per_page

    total_row = q1(
        "SELECT COUNT(*) AS c "
        "FROM task_tags tt "
        "JOIN tasks t ON t.id=tt.task_id "
        "WHERE tt.tag_id=%s "
        "AND t.recurrence_rule IS NOT NULL "
        "AND TRIM(t.recurrence_rule) <> ''",
        (tag_id,),
    )
    total = int(total_row["c"]) if total_row else 0
    pages = max(1, (total + per_page - 1) // per_page)

    if page > pages:
        page = pages
        offset = (page - 1) * per_page

    rows = q(
        "SELECT t.id, t.title, t.due_date, t.due_time, t.recurrence_rule, "
        "p.id AS project_id, p.name AS project_name, "
        "fd.id AS folder_id, fd.name AS folder_name "
        "FROM task_tags tt "
        "JOIN tasks t ON t.id=tt.task_id "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=COALESCE(t.folder_id, p.folder_id) "
        "WHERE tt.tag_id=%s "
        "AND t.recurrence_rule IS NOT NULL "
        "AND TRIM(t.recurrence_rule) <> '' "
        "ORDER BY (t.due_date IS NULL) ASC, t.due_date ASC, t.id DESC "
        "LIMIT %s OFFSET %s",
        (tag_id, per_page, offset),
    )

    task_ids = [int(r["id"]) for r in rows]
    runs_map: Dict[int, List[Dict[str, Any]]] = {}

    if task_ids:
        placeholders = ",".join(["%s"] * len(task_ids))
        run_rows = q(
            f"SELECT task_id, executed_at, previous_due_date, next_due_date "
            f"FROM recurring_task_runs "
            f"WHERE task_id IN ({placeholders}) "
            f"ORDER BY executed_at DESC",
            tuple(task_ids),
        )
        for rr in run_rows:
            tid = int(rr["task_id"])
            runs_map.setdefault(tid, []).append(rr)

    return render_template(
        "periodic_history.html",
        tag=tag,
        rows=rows,
        runs_map=runs_map,
        page=page,
        pages=pages,
        total=total,
        per_page=per_page,
    )


@app.route("/tasks/<int:task_id>/periodic-history")
def task_periodic_history(task_id: int):
    task = q1(
        "SELECT t.id, t.title, t.due_date, t.due_time, t.recurrence_rule, "
        "p.id AS project_id, p.name AS project_name, "
        "fd.id AS folder_id, fd.name AS folder_name "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=COALESCE(t.folder_id, p.folder_id) "
        "WHERE t.id=%s",
        (task_id,),
    )
    if not task:
        abort(404)

    if not (task.get("recurrence_rule") and str(task.get("recurrence_rule")).strip()):
        flash("La tarea no es periódica.", "error")
        return redirect(safe_next_url(request.args.get("next"), "home"))

    per_page = cfg_int(["app", "pagination", "periodic_history_per_page"], default=20, min_v=5, max_v=500)
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    page = max(page, 1)
    offset = (page - 1) * per_page

    total_row = q1(
        "SELECT COUNT(*) AS c FROM recurring_task_runs WHERE task_id=%s",
        (task_id,),
    )
    total = int(total_row["c"]) if total_row else 0
    pages = max(1, (total + per_page - 1) // per_page)

    if page > pages:
        page = pages
        offset = (page - 1) * per_page

    runs = q(
        "SELECT executed_at, previous_due_date, next_due_date "
        "FROM recurring_task_runs "
        "WHERE task_id=%s "
        "ORDER BY executed_at DESC "
        "LIMIT %s OFFSET %s",
        (task_id, per_page, offset),
    )

    next_url = safe_next_url(request.args.get("next"), "home")

    return render_template(
        "task_periodic_history.html",
        task=task,
        runs=runs,
        page=page,
        pages=pages,
        total=total,
        per_page=per_page,
        next_url=next_url,
    )


from datetime import date, timedelta

@app.route("/dashboard")
def dashboard():
    today = _today_madrid()
    period_days = coerce_int(request.args.get("days", 30), default=30, min_v=7, max_v=180)
    period_start = today - timedelta(days=period_days - 1)
    # Lunes de la semana actual
    monday = today - timedelta(days=today.weekday())
    # Primer día del mes actual
    first_of_month = today.replace(day=1)

    # Criterio de métricas:
    # - ACTIVAS (backlog actual): excluyen archivadas y papelera.
    # - HISTORICAS (productividad): excluyen papelera, pero pueden incluir archivadas.
    total = q1(
        "SELECT COUNT(*) AS c "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE t.deleted_at IS NULL "
        "AND t.archived=0 "
        "AND (t.project_id IS NULL OR p.archived=0)"
    )["c"]
    open_tasks = q1(
        "SELECT COUNT(*) AS c "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE t.completed_at IS NULL "
        "AND t.deleted_at IS NULL "
        "AND t.archived=0 "
        "AND (t.project_id IS NULL OR p.archived=0)"
    )["c"]
    completed = q1("SELECT COUNT(*) AS c FROM tasks WHERE completed_at IS NOT NULL AND deleted_at IS NULL")["c"]
    inbox = q1(
        "SELECT COUNT(*) AS c "
        "FROM tasks "
        "WHERE project_id IS NULL "
        "AND completed_at IS NULL "
        "AND archived=0 "
        "AND deleted_at IS NULL"
    )["c"]
    trash_tasks = q1("SELECT COUNT(*) AS c FROM tasks WHERE deleted_at IS NOT NULL")["c"]
    archived_tasks_cnt = q1("SELECT COUNT(*) AS c FROM tasks WHERE archived=1 AND deleted_at IS NULL")["c"]

    # Proyectos activos excluyendo las carpetas Rutinas/Agenda/Sometime/ADTV/Seguimiento y sus subdirectorios.
    excluded_root_names = {"rutinas", "agenda", "sometime", "adtv", "seguimiento"}
    folder_rows = q("SELECT id, parent_id, name FROM folders")
    children_by_parent: Dict[Optional[int], List[int]] = {}
    excluded_folder_ids: set[int] = set()

    for fr in folder_rows:
        parent_id = fr.get("parent_id")
        children_by_parent.setdefault(parent_id, []).append(int(fr["id"]))
        if normalize_tag_key(fr.get("name") or "") in excluded_root_names:
            excluded_folder_ids.add(int(fr["id"]))

    stack = list(excluded_folder_ids)
    while stack:
        fid = stack.pop()
        for ch_id in children_by_parent.get(fid, []):
            if ch_id not in excluded_folder_ids:
                excluded_folder_ids.add(ch_id)
                stack.append(ch_id)

    if excluded_folder_ids:
        placeholders = ",".join(["%s"] * len(excluded_folder_ids))
        projects_cnt = q1(
            "SELECT COUNT(*) AS c "
            "FROM projects "
            "WHERE archived=0 AND deleted_at IS NULL "
            f"AND (folder_id IS NULL OR folder_id NOT IN ({placeholders}))",
            tuple(sorted(excluded_folder_ids)),
        )["c"]
    else:
        projects_cnt = q1(
            "SELECT COUNT(*) AS c FROM projects WHERE archived=0 AND deleted_at IS NULL"
        )["c"]

    archived_cnt = q1("SELECT COUNT(*) AS c FROM projects WHERE archived=1 AND deleted_at IS NULL")["c"]
    if excluded_folder_ids:
        placeholders = ",".join(["%s"] * len(excluded_folder_ids))
        pending_active = q1(
            "SELECT COUNT(*) AS c "
            "FROM tasks t "
            "LEFT JOIN projects p ON p.id=t.project_id "
            "WHERE t.completed_at IS NULL "
            "AND t.archived=0 "
            "AND t.deleted_at IS NULL "
            "AND (t.project_id IS NULL OR p.archived=0) "
            f"AND (t.folder_id IS NULL OR t.folder_id NOT IN ({placeholders})) "
            f"AND (t.project_id IS NULL OR p.folder_id IS NULL OR p.folder_id NOT IN ({placeholders}))",
            tuple(sorted(excluded_folder_ids)) + tuple(sorted(excluded_folder_ids)),
        )["c"]
    else:
        pending_active = q1(
            "SELECT COUNT(*) AS c "
            "FROM tasks t "
            "LEFT JOIN projects p ON p.id=t.project_id "
            "WHERE t.completed_at IS NULL "
            "AND t.archived=0 "
            "AND t.deleted_at IS NULL "
            "AND (t.project_id IS NULL OR p.archived=0)"
        )["c"]

    # --- ESTADÍSTICAS HISTORICAS DE COMPLETADOS ---
    comp_today = q1(
        "SELECT COUNT(*) AS c FROM ("
        "SELECT t.id FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE t.completed_at IS NOT NULL "
        "AND t.deleted_at IS NULL "
        "AND DATE(t.completed_at)=%s "
        "UNION ALL "
        "SELECT t.id FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE t.last_completed_at IS NOT NULL "
        "AND t.recurrence_rule IS NOT NULL AND TRIM(t.recurrence_rule)<>'' "
        "AND t.deleted_at IS NULL "
        "AND DATE(t.last_completed_at)=%s"
        ") AS completed_union",
        (today, today),
    )["c"]
    comp_week = q1(
        "SELECT COUNT(*) AS c FROM ("
        "SELECT t.id FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE t.completed_at IS NOT NULL "
        "AND t.deleted_at IS NULL "
        "AND DATE(t.completed_at)>=%s "
        "UNION ALL "
        "SELECT t.id FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE t.last_completed_at IS NOT NULL "
        "AND t.recurrence_rule IS NOT NULL AND TRIM(t.recurrence_rule)<>'' "
        "AND t.deleted_at IS NULL "
        "AND DATE(t.last_completed_at)>=%s"
        ") AS completed_union",
        (monday, monday),
    )["c"]
    comp_month = q1(
        "SELECT COUNT(*) AS c FROM ("
        "SELECT t.id FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE t.completed_at IS NOT NULL "
        "AND t.deleted_at IS NULL "
        "AND DATE(t.completed_at)>=%s "
        "UNION ALL "
        "SELECT t.id FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE t.last_completed_at IS NOT NULL "
        "AND t.recurrence_rule IS NOT NULL AND TRIM(t.recurrence_rule)<>'' "
        "AND t.deleted_at IS NULL "
        "AND DATE(t.last_completed_at)>=%s"
        ") AS completed_union",
        (first_of_month, first_of_month),
    )["c"]
    comp_period = q1(
        "SELECT COUNT(*) AS c FROM ("
        "SELECT t.id FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE t.completed_at IS NOT NULL "
        "AND t.deleted_at IS NULL "
        "AND DATE(t.completed_at)>=%s "
        "UNION ALL "
        "SELECT t.id FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE t.last_completed_at IS NOT NULL "
        "AND t.recurrence_rule IS NOT NULL AND TRIM(t.recurrence_rule)<>'' "
        "AND t.deleted_at IS NULL "
        "AND DATE(t.last_completed_at)>=%s"
        ") AS completed_union",
        (period_start, period_start),
    )["c"]
    created_period = q1(
        "SELECT COUNT(*) AS c FROM tasks WHERE deleted_at IS NULL AND DATE(created_at) >= %s",
        (period_start,),
    )["c"]
    close_rate = int(round((comp_period / created_period) * 100)) if created_period else 0

    focus_projects_today = q1(
        "SELECT COUNT(DISTINCT COALESCE(t.project_id, 0)) AS c "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE t.deleted_at IS NULL "
        "AND ((t.completed_at IS NOT NULL AND DATE(t.completed_at)=%s) "
        "OR (t.last_completed_at IS NOT NULL "
        "AND t.recurrence_rule IS NOT NULL AND TRIM(t.recurrence_rule)<>'' "
        "AND DATE(t.last_completed_at)=%s))",
        (today, today),
    )["c"]

    pending_age_row = q1(
        "SELECT AVG(DATEDIFF(%s, DATE(t.created_at))) AS avg_days, "
        "MAX(DATEDIFF(%s, DATE(t.created_at))) AS max_days "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE t.completed_at IS NULL "
        "AND t.archived=0 "
        "AND t.deleted_at IS NULL "
        "AND (t.project_id IS NULL OR p.archived=0)",
        (today, today),
    )
    pending_age_avg = int(round(float(pending_age_row["avg_days"]))) if pending_age_row and pending_age_row["avg_days"] is not None else 0
    pending_age_oldest = int(pending_age_row["max_days"]) if pending_age_row and pending_age_row["max_days"] is not None else 0

    habits_today_row = q1(
        "SELECT "
        "(SELECT COUNT(*) "
        " FROM tasks t "
        " LEFT JOIN projects p ON p.id=t.project_id "
        " WHERE t.recurrence_rule IS NOT NULL "
        " AND TRIM(t.recurrence_rule)<>'' "
        " AND t.archived=0 "
        " AND t.deleted_at IS NULL "
        " AND (t.project_id IS NULL OR p.archived=0) "
        " AND t.due_date=%s) AS due_today, "
        "(SELECT COUNT(*) "
        " FROM recurring_task_runs rr "
        " JOIN tasks t ON t.id=rr.task_id "
        " LEFT JOIN projects p ON p.id=t.project_id "
        " WHERE rr.previous_due_date=%s "
        " AND DATE(rr.executed_at)=%s "
        " AND t.archived=0 "
        " AND t.deleted_at IS NULL "
        " AND (t.project_id IS NULL OR p.archived=0)) AS done_today",
        (today, today, today),
    )
    habits_due_today = int(habits_today_row["due_today"] or 0) if habits_today_row else 0
    habits_done_today = int(habits_today_row["done_today"] or 0) if habits_today_row else 0
    habits_planned_today = habits_due_today + habits_done_today
    habits_rate = int(round((habits_done_today / habits_planned_today) * 100)) if habits_planned_today else 0

    focus_split_row = q1(
        "SELECT "
        "SUM(CASE WHEN t.project_id IS NULL THEN 1 ELSE 0 END) AS reactive_cnt, "
        "SUM(CASE WHEN t.project_id IS NOT NULL THEN 1 ELSE 0 END) AS structured_cnt "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE t.completed_at IS NULL "
        "AND t.archived=0 "
        "AND t.deleted_at IS NULL "
        "AND (t.project_id IS NULL OR p.archived=0)"
    )
    reactive_active = int(focus_split_row["reactive_cnt"] or 0) if focus_split_row else 0
    structured_active = int(focus_split_row["structured_cnt"] or 0) if focus_split_row else 0
    structured_share = int(round((structured_active / (structured_active + reactive_active)) * 100)) if (structured_active + reactive_active) else 0

    avg_completion_row = q1(
        "SELECT ROUND(AVG(DATEDIFF(DATE(t.completed_at), DATE(t.created_at)))) AS avg_days "
        "FROM tasks t "
        "WHERE t.completed_at IS NOT NULL "
        "AND t.deleted_at IS NULL "
        "AND DATE(t.completed_at) >= %s",
        (period_start,),
    )
    avg_completion_days = int(avg_completion_row["avg_days"]) if avg_completion_row and avg_completion_row["avg_days"] is not None else 0

    overdue_rescue_today = int(q1(
        "SELECT COUNT(*) AS c FROM tasks "
        "WHERE completed_at IS NOT NULL "
        "AND deleted_at IS NULL "
        "AND DATE(completed_at) = %s "
        "AND due_date IS NOT NULL "
        "AND due_date < DATE(completed_at)",
        (today,),
    )["c"] or 0)
    overdue_rescue_week = int(q1(
        "SELECT COUNT(*) AS c FROM tasks "
        "WHERE completed_at IS NOT NULL "
        "AND deleted_at IS NULL "
        "AND DATE(completed_at) >= %s "
        "AND due_date IS NOT NULL "
        "AND due_date < DATE(completed_at)",
        (monday,),
    )["c"] or 0)

    comp_struct_row = q1(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN t.project_id IS NOT NULL THEN 1 ELSE 0 END) AS with_project "
        "FROM tasks t "
        "WHERE t.completed_at IS NOT NULL "
        "AND t.deleted_at IS NULL "
        "AND DATE(t.completed_at) >= %s",
        (monday,),
    )
    comp_total_week = int(comp_struct_row["total"] or 0) if comp_struct_row else 0
    comp_with_project_cnt = int(comp_struct_row["with_project"] or 0) if comp_struct_row else 0
    comp_project_share = int(round((comp_with_project_cnt / comp_total_week) * 100)) if comp_total_week else 0

    # --- TENDENCIA DIARIA DE COMPLETADAS ---
    trend_rows = q(
        "SELECT DATE(completed_at) AS d, COUNT(*) AS c "
        "FROM tasks "
        "WHERE completed_at IS NOT NULL "
        "AND deleted_at IS NULL "
        "AND DATE(completed_at) >= %s "
        "GROUP BY DATE(completed_at) "
        "ORDER BY d ASC",
        (period_start,),
    )
    trend_map = {r["d"]: int(r["c"]) for r in trend_rows}
    trend_values: List[int] = []
    trend_labels: List[str] = []
    for i in range(period_days):
        d = period_start + timedelta(days=i)
        trend_values.append(trend_map.get(d, 0))
        trend_labels.append(d.strftime("%d/%m"))

    svg_w, svg_h = 920, 230
    pad_l, pad_r, pad_t, pad_b = 32, 8, 12, 34
    plot_w = svg_w - pad_l - pad_r
    plot_h = svg_h - pad_t - pad_b
    vmax = max(max(trend_values, default=0), 1)
    n_points = max(1, len(trend_values))

    trend_points: List[Dict[str, Any]] = []
    for idx, val in enumerate(trend_values):
        x = pad_l + (plot_w * idx / (n_points - 1 if n_points > 1 else 1))
        y = pad_t + (plot_h * (1 - (val / vmax)))
        trend_points.append({
            "x": round(x, 2),
            "y": round(y, 2),
            "value": val,
            "label": trend_labels[idx],
        })
    trend_polyline = " ".join(f"{p['x']},{p['y']}" for p in trend_points)
    y_grid = [round(pad_t + (plot_h * i / 5), 2) for i in range(6)]
    axis_y = round(pad_t + plot_h, 2)

    tick_count = 5 if period_days <= 30 else 7
    tick_indices = sorted({
        int(round(i * (n_points - 1) / (tick_count - 1)))
        for i in range(tick_count)
    })
    x_ticks = [
        {
            "x": trend_points[i]["x"],
            "label": trend_points[i]["label"],
        }
        for i in tick_indices
    ]

    # --- TOP PROYECTOS COMPLETADOS EN PERIODO ---
    completed_by_project = q(
        "SELECT p.id AS id, COALESCE(p.name, 'Inbox') AS name, COUNT(*) AS c "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE t.completed_at IS NOT NULL "
        "AND t.deleted_at IS NULL "
        "AND DATE(t.completed_at) >= %s "
        "GROUP BY p.id, COALESCE(p.name, 'Inbox') "
        "ORDER BY c DESC, name ASC "
        "LIMIT 6",
        (period_start,),
    )
    project_max = max([int(r["c"]) for r in completed_by_project], default=1)

    # --- TOP ETIQUETAS EN PERIODO ---
    top_tags = q(
        "SELECT tg.id AS id, tg.name AS name, COUNT(*) AS c "
        "FROM task_tags tt "
        "INNER JOIN tags tg ON tg.id=tt.tag_id "
        "INNER JOIN tasks t ON t.id=tt.task_id "
        "WHERE t.completed_at IS NOT NULL "
        "AND t.deleted_at IS NULL "
        "AND DATE(t.completed_at) >= %s "
        "GROUP BY tg.id, tg.name "
        "ORDER BY c DESC, tg.name ASC "
        "LIMIT 10",
        (period_start,),
    )

    # --- CONSULTA MEJORADA DE VENCIMIENTOS (CON UBICACIÓN) ---
    due_soon = q(
        """
        SELECT t.id, t.title, t.notes, t.due_date, t.project_id, t.folder_id, t.priority,
               p.name AS project_name, f.name AS folder_name
        FROM tasks t
        LEFT JOIN projects p ON t.project_id = p.id
        LEFT JOIN folders f ON t.folder_id = f.id
        WHERE t.completed_at IS NULL 
                    AND t.archived = 0
                    AND t.deleted_at IS NULL
          AND t.due_date IS NOT NULL 
          AND t.due_date <= %s 
                    AND (t.project_id IS NULL OR p.archived = 0)
        ORDER BY t.due_date ASC 
        """,
        (today + timedelta(days=7),)
    )
    due_soon_task_ids = [t["id"] for t in due_soon]
    due_soon_tags_map = load_tags_map(due_soon_task_ids) if due_soon_task_ids else {}

    return render_template(
        "dashboard.html",
        stats={
            "total": total,
            "open": open_tasks,
            "completed": completed,
            "inbox": inbox,
            "trash_tasks": trash_tasks,
            "archived_tasks": archived_tasks_cnt,
            "projects": projects_cnt,
            "archived": archived_cnt,
            "archived_projects": archived_cnt,
            "comp_today": comp_today,
            "comp_week": comp_week,
            "comp_month": comp_month,
            "comp_period": comp_period,
            "close_rate": close_rate,
            "pending_active": pending_active,
        },
        focus={
            "projects_today": int(focus_projects_today or 0),
            "pending_age_avg": pending_age_avg,
            "pending_age_oldest": pending_age_oldest,
            "habits_done_today": habits_done_today,
            "habits_planned_today": habits_planned_today,
            "habits_rate": habits_rate,
            "reactive_active": reactive_active,
            "structured_active": structured_active,
            "structured_share": structured_share,
            "avg_completion_days": avg_completion_days,
            "overdue_rescue_today": overdue_rescue_today,
            "overdue_rescue_week": overdue_rescue_week,
            "comp_project_share": comp_project_share,
            "comp_with_project": comp_with_project_cnt,
            "comp_total_week": comp_total_week,
        },
        period_days=period_days,
        trend_points=trend_points,
        trend_polyline=trend_polyline,
        y_grid=y_grid,
        axis_y=axis_y,
        x_ticks=x_ticks,
        trend_max=vmax,
        completed_by_project=completed_by_project,
        project_max=project_max,
        top_tags=top_tags,
        today_d=today,
        due_soon=due_soon,
        due_soon_tags_map=due_soon_tags_map,
    )


@app.route("/dashboard/completed")
def dashboard_completed():
    period = (request.args.get("period") or "today").strip().lower()
    if period not in ("today", "week", "month"):
        period = "today"

    per_page = cfg_int(["app", "pagination", "search_per_page"], default=25, min_v=5, max_v=500)
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    page = max(page, 1)

    today = _today_madrid()
    monday = today - timedelta(days=today.weekday())
    first_of_month = today.replace(day=1)

    if period == "today":
        where_sql = "DATE(t.completed_at) = %s"
        where_params: Tuple[Any, ...] = (today,)
        title = "Tareas realizadas hoy"
        subtitle = today.strftime("%d-%m-%Y")
    elif period == "week":
        where_sql = "DATE(t.completed_at) >= %s"
        where_params = (monday,)
        title = "Tareas realizadas esta semana"
        subtitle = f"Desde {monday.strftime('%d-%m-%Y')}"
    else:
        where_sql = "DATE(t.completed_at) >= %s"
        where_params = (first_of_month,)
        title = "Tareas realizadas este mes"
        subtitle = first_of_month.strftime("Desde %d-%m-%Y")

    total_row = q1(
        "SELECT COUNT(*) AS c FROM ("
        "SELECT t.id FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE t.completed_at IS NOT NULL "
        "AND t.deleted_at IS NULL "
        f"AND {where_sql} "
        "UNION ALL "
        "SELECT t.id FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE t.last_completed_at IS NOT NULL "
        "AND t.recurrence_rule IS NOT NULL AND TRIM(t.recurrence_rule) <> '' "
        "AND t.deleted_at IS NULL "
        f"AND {where_sql.replace('t.completed_at', 't.last_completed_at')} "
        ") AS combined_tasks",
        where_params + where_params,
    )
    total = int(total_row["c"]) if total_row else 0
    pages = max(1, (total + per_page - 1) // per_page)
    if page > pages:
        page = pages
    offset = (page - 1) * per_page

    rows = q(
        "SELECT * FROM ("
        "SELECT t.id, t.title, t.notes, t.due_date, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=COALESCE(t.folder_id, p.folder_id) "
        "WHERE t.completed_at IS NOT NULL "
        "AND t.deleted_at IS NULL "
        f"AND {where_sql} "
        "UNION ALL "
        "SELECT t.id, t.title, t.notes, t.due_date, t.last_completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=COALESCE(t.folder_id, p.folder_id) "
        "WHERE t.last_completed_at IS NOT NULL "
        "AND t.recurrence_rule IS NOT NULL AND TRIM(t.recurrence_rule) <> '' "
        "AND t.deleted_at IS NULL "
        f"AND {where_sql.replace('t.completed_at', 't.last_completed_at')} "
        ") AS completed_union "
        "ORDER BY completed_at DESC, id DESC "
        "LIMIT %s OFFSET %s",
        where_params + where_params + (per_page, offset),
    )

    task_ids = [r["id"] for r in rows]
    tags_map = load_tags_map(task_ids) if task_ids else {}

    return render_template(
        "dashboard_completed.html",
        period=period,
        title=title,
        subtitle=subtitle,
        rows=rows,
        tags_map=tags_map,
        total=total,
        page=page,
        pages=pages,
        per_page=per_page,
    )


@app.route("/dashboard/pending-active")
def dashboard_pending_active():
    per_page = cfg_int(["app", "pagination", "search_per_page"], default=25, min_v=5, max_v=500)
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    page = max(page, 1)

    excluded_root_names = {"rutinas", "agenda", "sometime", "adtv", "seguimiento"}
    folder_rows = q("SELECT id, parent_id, name FROM folders")
    children_by_parent: Dict[Optional[int], List[int]] = {}
    excluded_folder_ids: set[int] = set()

    for fr in folder_rows:
        parent_id = fr.get("parent_id")
        children_by_parent.setdefault(parent_id, []).append(int(fr["id"]))
        if normalize_tag_key(fr.get("name") or "") in excluded_root_names:
            excluded_folder_ids.add(int(fr["id"]))

    stack = list(excluded_folder_ids)
    while stack:
        fid = stack.pop()
        for ch_id in children_by_parent.get(fid, []):
            if ch_id not in excluded_folder_ids:
                excluded_folder_ids.add(ch_id)
                stack.append(ch_id)

    base_from_sql = (
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
    )
    base_where_sql = (
        "WHERE t.completed_at IS NULL "
        "AND t.archived=0 "
        "AND t.deleted_at IS NULL "
        "AND (t.project_id IS NULL OR p.archived=0)"
    )
    filter_sql = ""
    filter_params: Tuple[Any, ...] = ()
    if excluded_folder_ids:
        placeholders = ",".join(["%s"] * len(excluded_folder_ids))
        filter_sql = (
            f" AND (t.folder_id IS NULL OR t.folder_id NOT IN ({placeholders}))"
            f" AND (t.project_id IS NULL OR p.folder_id IS NULL OR p.folder_id NOT IN ({placeholders}))"
        )
        ids_sorted = tuple(sorted(excluded_folder_ids))
        filter_params = ids_sorted + ids_sorted

    total_row = q1(
        "SELECT COUNT(*) AS c " + base_from_sql + base_where_sql + filter_sql,
        filter_params,
    )
    total = int(total_row["c"]) if total_row else 0
    pages = max(1, (total + per_page - 1) // per_page)
    if page > pages:
        page = pages
    offset = (page - 1) * per_page

    rows = q(
        "SELECT t.id, t.title, t.notes, t.due_date, t.due_time, t.priority, t.created_at, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        + base_from_sql
        + "LEFT JOIN folders fd ON fd.id=COALESCE(t.folder_id, p.folder_id) "
        + base_where_sql
        + filter_sql
        + " ORDER BY (t.due_date IS NULL) ASC, t.due_date ASC, (t.due_time IS NULL) ASC, t.due_time ASC, t.id DESC "
        + "LIMIT %s OFFSET %s",
        filter_params + (per_page, offset),
    )

    task_ids = [r["id"] for r in rows]
    tags_map = load_tags_map(task_ids) if task_ids else {}

    return render_template(
        "dashboard_pending_active.html",
        title="Tareas pendientes activas",
        subtitle="Excluye Rutinas, Agenda, Sometime, ADTV y Seguimiento (incluye subcarpetas)",
        rows=rows,
        tags_map=tags_map,
        total=total,
        page=page,
        pages=pages,
        per_page=per_page,
    )

@app.route("/import")
def import_view():
    return render_template("import.html")


@app.route("/manual")
def manual_legacy():
    return redirect(url_for("manual"), code=302)


@app.route("/gtdApp/manual")
def manual():
    return render_template("manual/index.html", title="Manual de usuario")


@app.route("/gtdApp/manual/filtros")
def manual_filters():
    return render_template("manual/filters.html", title="Manual: Filtros")


@app.route("/gtdApp/manual/gmail")
def manual_gmail():
    return render_template("manual/gmail.html", title="Manual: Integración con Gmail")


@app.route("/gtdApp/manual/google-calendar")
def manual_google_calendar():
    return render_template("manual/google_calendar.html", title="Manual: Integración con Google Calendar")


@app.route("/gtdApp/manual/telegram")
def manual_telegram():
    return render_template("manual/telegram.html", title="Manual: Integración con Telegram")


@app.route("/gtdApp/manual/despliegue")
def manual_deployment():
    return render_template("manual/deployment.html", title="Manual: Instalación y despliegue")


# ---------------- Routes: create/edit tasks ----------------

@app.route("/tasks/create", methods=["POST"])
def task_create():
    import re

    raw = (request.form.get("quick") or "").strip()
    due = request.form.get("due_date") or None
    due_time_raw = (request.form.get("due_time") or "").strip()
    recurrence = (request.form.get("recurrence_rule") or "").strip() or None
    priority = coerce_priority(request.form.get("priority"), default=None)

    if not raw:
        flash("El nombre de la tarea es obligatorio.", "error")
        return redirect(request.referrer or url_for("home"))

    raw_work = raw

    # 1) Extraer etiquetas tipo @etiqueta
    tags = re.findall(r'@([^\s@#]+)', raw_work)

    # 2) Extraer fecha desde texto rápido
    detected_due_date = None
    detected_due_time = None
    detected_priority = None
    try:
        detected_priority, raw_work = extract_priority_from_quick(raw_work)
        if not due:
            detected_due_date, raw_work = extract_due_date_from_quick(raw_work)
        if not due_time_raw:
            detected_due_time, raw_work = extract_due_time_from_quick(raw_work)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(request.referrer or url_for("home"))

    # 3) Extraer recurrencia desde texto natural
    if not recurrence:
        for pattern, rule in RECURRENCE_PATTERNS.items():
            if re.search(pattern, raw_work, flags=re.IGNORECASE):
                recurrence = rule
                break

    # 4) Extraer posible proyecto desde #texto o #"texto con espacios"
    project_name = None
    m_proj = PROJ_RE.search(raw_work)
    if m_proj:
        candidate_name = (m_proj.group(1) or m_proj.group(2) or '').strip()
        if not re.fullmatch(r'\d{2}-\d{2}-\d{4}', candidate_name):
            project_name = candidate_name

    # 4b) Extraer posible carpeta desde f:texto o f:"texto con espacios"
    folder_name_quick = None
    m_folder = FOLDER_RE.search(raw_work)
    if m_folder:
        folder_name_quick = (m_folder.group(1) or m_folder.group(2) or '').strip()

    # 5) Limpiar el texto para obtener el título real
    title = raw_work
    title = re.sub(r'@([^\s@#]+)', '', title)
    # title = re.sub(r'#\d{2}-\d{2}-\d{4}\b', '', title)
    title = re.sub(r'\bcada\s+dia\b', '', title, flags=re.IGNORECASE)
    title = re.sub(r'\bcada\s+semana\b', '', title, flags=re.IGNORECASE)
    title = re.sub(r'\bcada\s+mes\b', '', title, flags=re.IGNORECASE)
    title = re.sub(r'\bcada\s+año\b', '', title, flags=re.IGNORECASE)
    title = PROJ_RE.sub('', title)
    title = FOLDER_RE.sub('', title)
    title = re.sub(r'\s+', ' ', title).strip(" -_,.;:")

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
        detected_priority, raw = extract_priority_from_quick(raw)
        due_date, raw = extract_due_date_from_quick(raw)
        detected_due_time = None
        if not due_time_raw:
            detected_due_time, raw = extract_due_time_from_quick(raw)
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
        due_time = detected_due_time

    if detected_priority is not None:
        priority = detected_priority

    title, tags, quick_project_name, quick_folder_name = parse_task_quick_entry(raw)
    
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
            "INSERT INTO tasks(title, project_id, folder_id, due_date, due_time, priority, sort_order) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (
                title,
                project_id,
                folder_id,
                due_date,
                due_time,
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
    

from urllib.parse import urlparse
from datetime import datetime

@app.route("/tasks/<int:task_id>/edit", methods=["GET", "POST"])
def task_edit(task_id: int):
    # ---------- helpers ----------
    # Reutilizamos safe_next_url global para consistencia entre rutas.

    # ---------- load task ----------
    task = q1(
        "SELECT id, title, notes, due_date, due_time, project_id, folder_id, recurrence_rule, priority, completed_at "
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
        "FROM task_tags tt JOIN tags tg ON tg.id=tt.tag_id "
        "WHERE tt.task_id=%s ORDER BY tg.name",
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
        due_raw = (request.form.get("due_date") or "").strip()
        due_time_raw = (request.form.get("due_time") or "").strip()
        recurrence = (request.form.get("recurrence_rule") or "").strip() or None
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
                "SET title=%s, notes=%s, due_date=%s, due_time=%s, project_id=%s, folder_id=%s, recurrence_rule=%s, priority=%s "
                "WHERE id=%s",
                (clean_title, notes, due_date, due_time, project_id, folder_id, recurrence, priority, task_id),
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



@app.route("/tasks/<int:task_id>/toggle", methods=["POST"])
def task_toggle(task_id: int):
    task = q1(
        "SELECT id, completed_at, due_date, recurrence_rule, project_id, archived "
        "FROM tasks WHERE id=%s",
        (task_id,),
    )
    if not task:
        abort(404)

    now = datetime.now(ZoneInfo("Europe/Madrid")).replace(tzinfo=None)  # guardamos naive en DB

    try:
        if task["completed_at"]:
            # Desmarcar: funciona exactamente como ahora
            exec_sql("UPDATE tasks SET completed_at=NULL WHERE id=%s", (task_id,))

        else:
            # CASO 1: tarea recurrente -> comportamiento actual, sin tocar NextAction
            if task.get("recurrence_rule") and task.get("due_date"):
                rule = parse_rrule(task["recurrence_rule"])
                previous_due = task["due_date"]
                next_due = next_due_date(task["due_date"], rule)
                recurrence_due_choice = (request.form.get("recurrence_due_choice") or "").strip().lower()
                today_d = now.date()

                if next_due < today_d and recurrence_due_choice != "keep":
                    next_due = next_due_date_after_today(task["due_date"], rule, today_d)
                exec_sql(
                    "UPDATE tasks SET last_completed_at=%s, due_date=%s, completed_at=NULL WHERE id=%s",
                    (now, next_due, task_id),
                )
                exec_sql(
                    "INSERT INTO recurring_task_runs(task_id, executed_at, previous_due_date, next_due_date) "
                    "VALUES(%s, %s, %s, %s)",
                    (task_id, now, previous_due, next_due),
                )
                # Resetear todas las subtareas para el nuevo ciclo
                exec_sql(
                    "UPDATE subtasks SET completed_at=NULL WHERE task_id=%s",
                    (task_id,),
                )

            else:
                # CASO 2: tarea no recurrente
                has_nextaction = q1(
                    "SELECT 1 AS ok "
                    "FROM task_tags tt "
                    "JOIN tags tg ON tg.id=tt.tag_id "
                    "WHERE tt.task_id=%s AND LOWER(tg.name)=LOWER(%s) "
                    "LIMIT 1",
                    (task_id, "NextAction"),
                ) is not None

                # Si tenía NextAction, quitársela antes de completar (en cualquier variante de mayúsculas/minúsculas)
                if has_nextaction:
                    exec_sql(
                        "DELETE tt FROM task_tags tt "
                        "JOIN tags tg ON tg.id=tt.tag_id "
                        "WHERE tt.task_id=%s AND LOWER(tg.name)=LOWER(%s)",
                        (task_id, "NextAction"),
                    )

                # Marcar como hecha
                exec_sql("UPDATE tasks SET completed_at=%s WHERE id=%s", (now, task_id))

                # Si tenía NextAction y pertenece a un proyecto, promocionar la siguiente
                promote_nextaction = cfg_bool(
                    ["app", "behavior", "promote_nextaction_on_complete"],
                    default=True,
                )
                # Override a nivel de proyecto (si está definido) tiene prioridad sobre el global
                if has_nextaction and task.get("project_id"):
                    _proj_promo = q1(
                        "SELECT auto_promote_nextaction FROM projects WHERE id=%s",
                        (task["project_id"],),
                    )
                    if _proj_promo and _proj_promo.get("auto_promote_nextaction") is not None:
                        promote_nextaction = bool(_proj_promo["auto_promote_nextaction"])

                if promote_nextaction and has_nextaction and task.get("project_id"):
                    next_task = q1(
                        "SELECT id "
                        "FROM tasks "
                        "WHERE project_id=%s "
                        "AND archived=0 "
                        "AND completed_at IS NULL "
                        "AND id<>%s "
                        "ORDER BY (sort_order IS NULL) ASC, sort_order ASC, (due_date IS NULL) ASC, due_date ASC, id ASC "
                        "LIMIT 1",
                        (task["project_id"], task_id),
                    )
                    if next_task:
                        next_has_nextaction = q1(
                            "SELECT 1 AS ok "
                            "FROM task_tags tt "
                            "JOIN tags tg ON tg.id=tt.tag_id "
                            "WHERE tt.task_id=%s AND LOWER(tg.name)=LOWER(%s) "
                            "LIMIT 1",
                            (next_task["id"], "NextAction"),
                        ) is not None
                        if not next_has_nextaction:
                            next_tag = q1(
                                "SELECT id FROM tags WHERE LOWER(name)=LOWER(%s) ORDER BY id ASC LIMIT 1",
                                ("NextAction",),
                            )
                            next_tag_id = int(next_tag["id"]) if next_tag else get_or_create_tag("NextAction")
                            exec_sql(
                                "INSERT IGNORE INTO task_tags(task_id, tag_id) VALUES(%s,%s)",
                                (next_task["id"], next_tag_id),
                            )

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


# ---------------- Routes: projects / folders CRUD ----------------

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

    folder_breadcrumb = build_folder_breadcrumb(folder_id, include_self=False)

    # Proyectos dentro de la carpeta (activos)
    projects = q(
        "SELECT id, name FROM projects WHERE folder_id=%s AND archived=0 AND deleted_at IS NULL ORDER BY name",
        (folder_id,)
    )

    # Tareas asignadas directamente a la carpeta (no a proyectos)
    tasks = q(
        "SELECT t.id, t.title, t.notes, t.due_date, t.due_time, t.completed_at, t.recurrence_rule, t.priority "
        "FROM tasks t "
        "WHERE t.folder_id=%s AND t.project_id IS NULL AND t.archived=0 AND t.deleted_at IS NULL AND t.completed_at IS NULL "
        "ORDER BY (t.due_date IS NULL) ASC, t.due_date ASC, t.id DESC",
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
        "SELECT t.id, t.title, t.notes, t.due_date, t.completed_at, t.recurrence_rule, t.priority "
        "FROM tasks t "
        "WHERE t.folder_id=%s AND t.project_id IS NULL AND t.deleted_at IS NULL "
        "AND t.completed_at IS NOT NULL AND t.archived=0 "
        "UNION ALL "
        "SELECT t.id, t.title, t.notes, t.due_date, t.last_completed_at, t.recurrence_rule, t.priority "
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


@app.route("/inbox/archive_completed_tasks", methods=["POST"])
def inbox_archive_completed_tasks():
    periodic_names = ("periodica", "periódica", "periodicas", "periódicas")
    try:
        total_row = q1(
            "SELECT COUNT(*) AS c "
            "FROM tasks t "
            "WHERE t.folder_id IS NULL "
            "AND t.project_id IS NULL "
            "AND t.archived=0 "
            "AND t.deleted_at IS NULL "
            "AND t.completed_at IS NOT NULL "
            "AND (t.recurrence_rule IS NULL OR TRIM(t.recurrence_rule)='') "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM task_tags tt "
            "  JOIN tags tg ON tg.id=tt.tag_id "
            "  WHERE tt.task_id=t.id "
            "  AND LOWER(tg.name) IN (LOWER(%s), LOWER(%s), LOWER(%s), LOWER(%s))"
            ")",
            periodic_names,
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
                "    WHERE t.folder_id IS NULL "
                "    AND t.project_id IS NULL "
                "    AND t.archived=0 "
                "    AND t.deleted_at IS NULL "
                "    AND t.completed_at IS NOT NULL "
                "    AND (t.recurrence_rule IS NULL OR TRIM(t.recurrence_rule)='') "
                "    AND NOT EXISTS ("
                "      SELECT 1 FROM task_tags tt "
                "      JOIN tags tg ON tg.id=tt.tag_id "
                "      WHERE tt.task_id=t.id "
                "      AND LOWER(tg.name) IN (LOWER(%s), LOWER(%s), LOWER(%s), LOWER(%s))"
                "    )"
                "  ) AS eligible"
                ")",
                periodic_names,
            )

        commit()
        flash(f"{total_to_archive} tareas realizadas archivadas.", "ok")
    except Exception as e:
        rollback()
        flash(f"No se pudieron archivar las tareas realizadas: {e}", "error")

    return redirect(url_for("home"))


@app.route("/inbox/empty", methods=["POST"])
def inbox_empty():
    try:
        total_row = q1(
            "SELECT COUNT(*) AS c "
            "FROM tasks "
            "WHERE folder_id IS NULL "
            "AND project_id IS NULL "
            "AND deleted_at IS NULL",
        )
        total_to_trash = int(total_row["c"]) if total_row else 0

        if total_to_trash > 0:
            exec_sql(
                "UPDATE tasks "
                "SET deleted_prev_archived=archived, "
                "deleted_at=NOW(), "
                "archived=1, "
                "archived_at=COALESCE(archived_at, NOW()), "
                "calendar_local_changed_at=NOW(), "
                "calendar_sync_state=CASE WHEN google_event_id IS NOT NULL THEN 'pending_delete' ELSE 'none' END "
                "WHERE folder_id IS NULL "
                "AND project_id IS NULL "
                "AND deleted_at IS NULL",
            )

        commit()
        flash(f"{total_to_trash} tareas del Inbox enviadas a la papelera.", "ok")
    except Exception as e:
        rollback()
        flash(f"No se pudo vaciar el Inbox: {e}", "error")

    return redirect(url_for("home"))


@app.route("/folders/<int:folder_id>/archive_completed_tasks", methods=["POST"])
def folder_archive_completed_tasks(folder_id: int):
    folder = q1("SELECT id, name FROM folders WHERE id=%s", (folder_id,))
    if not folder:
        abort(404)
    periodic_names = ("periodica", "periódica", "periodicas", "periódicas")

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
            "  AND LOWER(tg.name) IN (LOWER(%s), LOWER(%s), LOWER(%s), LOWER(%s))"
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
                "      AND LOWER(tg.name) IN (LOWER(%s), LOWER(%s), LOWER(%s), LOWER(%s))"
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

    try:
        ast = parse_filter_expression(flt["expression"])

        # Por defecto: solo tareas abiertas
        if not ast_contains_done(ast):
            ast = And(ast, Not(Term("IDENT", "done")))

        where_sql, params = compile_filter_to_sql(ast)
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
        f"WHERE {where_sql} AND t.archived=0 AND t.deleted_at IS NULL AND (t.project_id IS NULL OR p.archived = 0)",
        tuple(params),
    )
    total = int(total_row["c"]) if total_row else 0
    pages = max(1, (total + per_page - 1) // per_page)

    # Página de resultados
    sql = (
        "SELECT t.id, t.title, t.notes, t.due_date, t.due_time, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "LEFT JOIN folders pf ON pf.id=p.folder_id "
        f"WHERE {where_sql} AND t.archived=0 AND t.deleted_at IS NULL AND (t.project_id IS NULL OR p.archived = 0) "
        "ORDER BY (t.completed_at IS NOT NULL) ASC, (t.due_date IS NULL) ASC, t.due_date ASC, t.id DESC "
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
    

# ---------------- Admin ----------------

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


def _load_calendar_sync_view_data(page: int, only_conflicts: bool = False) -> Dict[str, Any]:
    per_page = cfg_int(["app", "pagination", "calendar_conflicts_per_page"], default=25, min_v=5, max_v=500)

    base_where = "calendar_sync_state IN ('pending_push','pending_delete','conflict','error')"
    if only_conflicts:
        base_where = "calendar_sync_state='conflict'"

    total_row = q1(f"SELECT COUNT(*) AS c FROM tasks WHERE {base_where}")
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
        "calendar_sync_state, calendar_sync_error, calendar_conflict_at, calendar_conflict_payload "
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
        (per_page, offset),
    )

    calendar_sync_items: List[Dict[str, Any]] = []
    for r in rows:
        state = (r.get("calendar_sync_state") or "").strip().lower()
        if state == "conflict":
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
        pending_count_row = q1("SELECT COUNT(*) AS c FROM calendar_pending_events WHERE state='pending'")
        pending_gcal_total = int((pending_count_row or {}).get("c") or 0)
        pending_rows = q(
            "SELECT id, google_event_id, title, due_date, due_time, "
            "TIME_FORMAT(due_time, '%%H:%%i') AS due_time_text, notes, discovered_at "
            "FROM calendar_pending_events "
            "WHERE state='pending' "
            "ORDER BY discovered_at DESC "
            "LIMIT 200"
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
                "state_label": "Sin importar",
                "action_defs": [
                    {"key": "import_gtd", "label": "Crear en GTD"},
                    {"key": "solo_gcal", "label": "Solo GCal"},
                ],
                "actionable": True,
                "reason": "Evento de Google Calendar sin tarea GTD asociada",
                "default_action_key": "import_gtd",
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
                "SELECT id, calendar_sync_state, calendar_conflict_payload "
                "FROM tasks "
                f"WHERE id IN ({placeholders})",
                tuple(task_ids),
            )

        state_by_id: Dict[int, str] = {int(r["id"]): (r.get("calendar_sync_state") or "") for r in rows}
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
                        "FROM calendar_pending_events WHERE id=%s AND state='pending'",
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
                    elif chosen == "solo_gcal":
                        exec_sql(
                            "UPDATE calendar_pending_events SET state='ignored' WHERE id=%s",
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
                "SELECT t.id, t.project_id, t.folder_id, t.title, t.notes, t.due_date, t.due_time, "
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

def admin_required() -> bool:
    pwd = os.environ.get("GTD_ADMIN_PASSWORD", "")
    if not pwd:
        return False
    return session.get("is_admin") is True


def get_api_token_owner_user_id() -> int:
    user_id = session.get("user_id")
    try:
        return int(user_id) if user_id is not None else 1
    except (TypeError, ValueError):
        return 1


def next_project_task_sort_order(project_id: int) -> int:
    row = q1(
        "SELECT COALESCE(MAX(sort_order), 0) + 1 AS next_order "
        "FROM tasks WHERE project_id=%s AND deleted_at IS NULL",
        (project_id,),
    )
    return int((row or {}).get("next_order") or 1)


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

    context = _load_calendar_sync_view_data(page=page, only_conflicts=only_conflicts)
    calendar_sync_last_info = session.pop("calendar_sync_last_info", None)
    calendar_sync_last_level = session.pop("calendar_sync_last_level", "ok")
    return render_template(
        "calendar_sync.html",
        calendar_sync_last_info=calendar_sync_last_info,
        calendar_sync_last_level=calendar_sync_last_level,
        **context,
    )

# --- Card de gestión de tokens API para extensiones ---
@app.route("/admin/api_tokens", methods=["GET"])
def admin_api_tokens():
    if not admin_required():
        return redirect(url_for("admin"))
    user_id = get_api_token_owner_user_id()
    tokens = q(
        "SELECT id, device_name, token, created_at, last_used_at, active FROM api_tokens WHERE user_id=%s ORDER BY created_at DESC",
        (user_id,)
    )
    # Prefijo token para mostrar solo los primeros caracteres
    for t in tokens:
        t["token_prefix"] = t["token"][:6]
    return render_template("admin_api_tokens.html", tokens=tokens)

@app.route("/admin", methods=["GET", "POST"])
def admin():
    env_pwd_set = bool(os.environ.get("GTD_ADMIN_PASSWORD", ""))
    api_tokens = []

    if request.method == "POST":
        action = request.form.get("action", "")

        if action == "sync_calendar_now":
            handled = _handle_calendar_conflicts_action(action, "admin")
            if handled is not None:
                return handled

        if action == "backup_db":
               if not admin_required():
                   flash("No autorizado.", "error")
                   return redirect(url_for("admin"))

               BACKUP_DIR.mkdir(parents=True, exist_ok=True)
               ts = datetime.now().strftime("%Y%m%d_%H%M%S")
               fname = f"gtd_backup_{ts}.sql"
               path = BACKUP_DIR / fname

               ok, msg = run_mysqldump_to_file(path)
               flash(f"Copia creada: {fname}" if ok else f"No se pudo crear la copia: {msg}", "ok" if ok else "error")
               return redirect(url_for("admin"))

        if action == "restore_db":
              if not admin_required():
                  flash("No autorizado.", "error")
                  return redirect(url_for("admin"))

              fname = safe_backup_filename(request.form.get("backup_file", ""))
              if not fname:
                flash("Nombre de copia inválido.", "error")
                return redirect(url_for("admin"))

              path = BACKUP_DIR / fname
              if not path.exists():
                flash("La copia seleccionada no existe.", "error")
                return redirect(url_for("admin"))

              # Confirmación explícita (doble)
              if request.form.get("confirm", "") != "YES":
                flash("Restauración cancelada: debes confirmar escribiendo YES.", "error")
                return redirect(url_for("admin"))

              ok, msg = run_mysql_import_from_file(path)
              flash(f"Base de datos restaurada desde {fname}." if ok else f"No se pudo restaurar: {msg}", "ok" if ok else "error")
              return redirect(url_for("admin"))

        if action == "login":
            supplied = request.form.get("password", "")
            if env_pwd_set and supplied == os.environ.get("GTD_ADMIN_PASSWORD"):
                session["is_admin"] = True
                flash("Acceso admin concedido.", "ok")
            else:
                flash("Contraseña incorrecta o GTD_ADMIN_PASSWORD no definida.", "error")
            return redirect(url_for("admin"))

        if not admin_required():
            flash("No autorizado.", "error")
            return redirect(url_for("admin"))

        if action == "create_api_token":
            user_id = get_api_token_owner_user_id()
            device_name = (request.form.get("device_name") or "").strip() or "Navegador"
            try:
                token = create_api_token(user_id, device_name)
                flash(f"Token creado para {device_name}: {token}", "ok")
            except Exception as e:
                rollback()
                flash(f"No se pudo crear el token: {e}", "error")
            return redirect(url_for("admin"))

        if action == "revoke_api_token":
            token = (request.form.get("token") or "").strip()
            if not token:
                flash("Token inválido.", "error")
                return redirect(url_for("admin"))

            owned_token = q1(
                "SELECT id FROM api_tokens WHERE token=%s AND user_id=%s",
                (token, get_api_token_owner_user_id()),
            )
            if not owned_token:
                flash("El token no existe o no pertenece al usuario actual.", "error")
                return redirect(url_for("admin"))

            try:
                revoke_api_token(token)
                flash("Token revocado.", "ok")
            except Exception as e:
                rollback()
                flash(f"No se pudo revocar el token: {e}", "error")
            return redirect(url_for("admin"))

        if action == "save_config":
            cfg = load_config()
            cfg["db"]["host"] = request.form.get("db_host", cfg["db"]["host"])
            cfg["db"]["port"] = int(request.form.get("db_port", cfg["db"]["port"]))
            cfg["db"]["user"] = request.form.get("db_user", cfg["db"]["user"])
            cfg["db"]["password"] = request.form.get("db_password", cfg["db"]["password"])
            cfg["db"]["database"] = request.form.get("db_database", cfg["db"]["database"])
            
            # App / Pagination
            cfg.setdefault("app", {})
            cfg["app"].setdefault("pagination", {})

            def _read_int(name: str, fallback: int, min_v: int = 1, max_v: int = 500) -> int:
                raw = (request.form.get(name) or "").strip()
                try:
                    v = int(raw)
                except Exception:
                    v = fallback
                if v < min_v:
                    v = min_v
                if v > max_v:
                    v = max_v
                return v

            # ✅ Agenda (lo que pediste)
            cur_agenda = int(cfg["app"]["pagination"].get("agenda_per_page", 25) or 5)
            cfg["app"]["pagination"]["agenda_per_page"] = _read_int("agenda_per_page", cur_agenda, min_v=5, max_v=500)

            # (Opcional, por si ya quieres dejarlo cableado)
            cfg["app"]["pagination"]["search_per_page"] = _read_int(
                "search_per_page",
                int(cfg["app"]["pagination"].get("search_per_page", 25) or 25),
                min_v=5, max_v=500
            )
            cfg["app"]["pagination"]["tags_per_page"] = _read_int(
                "tags_per_page",
                int(cfg["app"]["pagination"].get("tags_per_page", 12) or 12),
                min_v=5, max_v=200
            )
            cfg["app"]["pagination"]["tag_detail_per_page"] = _read_int(
                "tag_detail_per_page",
                int(cfg["app"]["pagination"].get("tag_detail_per_page", 50) or 50),
                min_v=5, max_v=500
            )
            cfg["app"]["pagination"]["folders_per_page"] = _read_int(
                "folders_per_page",
                int(cfg["app"]["pagination"].get("folders_per_page", 10) or 10),
                min_v=5, max_v=200
            )
            cfg["app"]["pagination"]["filters_per_page"] = _read_int(
                "filters_per_page",
                int(cfg["app"]["pagination"].get("filters_per_page", 15) or 15),
                min_v=5, max_v=500
            )

            cfg["app"]["pagination"]["projects_per_page"] = _read_int(
                "projects_per_page",
                int(cfg["app"]["pagination"].get("projects_per_page", 15) or 15),
                min_v=5, max_v=500
            )
            
            save_config(cfg)
            flash("Configuración guardada.", "ok")
            return redirect(url_for("admin"))

        if action == "renew_google_token":
            creds_path = gmail_credentials_path()
            if not creds_path.exists():
                flash(
                    "Falta instance/gmail_credentials.json. Descarga credenciales OAuth y colócalas en instance/.",
                    "error",
                )
                return redirect(url_for("admin"))

            try:
                # Usar HTTPS para callback automático (Google lo requiere en producción)
                redirect_uri = "https://raspvinxeira.mooo.com:9999/gtdApp/admin/google_oauth/callback"
                flow = _build_admin_google_flow(redirect_uri)
                auth_url, state = flow.authorization_url(
                    access_type="offline",
                    prompt="consent",
                    include_granted_scopes="true",
                )
                session["google_oauth_state"] = state
                session["google_oauth_code_verifier"] = getattr(flow, "code_verifier", None)
                return redirect(auth_url)
            except Exception as e:
                flash(f"No se pudo iniciar la renovación OAuth: {e}", "error")
                return redirect(url_for("admin"))


        if action == "save_nextaction_behavior":
            cfg = load_config()
            appcfg = cfg.setdefault("app", {})
            behavior = appcfg.setdefault("behavior", {})

            raw = (request.form.get("promote_nextaction_on_complete") or "1").strip().lower()
            behavior["promote_nextaction_on_complete"] = raw not in ("0", "false", "no", "off")

            save_config(cfg)
            flash("Ajuste de NextAction guardado.", "ok")
            return redirect(url_for("admin"))

        if action == "initialize_system":
            if not admin_required():
                flash("No autorizado.", "error")
                return redirect(url_for("admin"))

            try:
                ensure_review_defaults()
                commit()
                flash("Sistema inicializado: etiquetas y carpetas creadas.", "ok")
            except Exception as e:
                rollback()
                flash(f"No se pudo inicializar el sistema: {e}", "error")

            return redirect(url_for("admin"))
        
        if action == "purge_unused_tags":
            if not admin_required():
                flash("No autorizado.", "error")
                return redirect(url_for("admin"))

            try:
                exec_sql(
                    "DELETE tg FROM tags tg "
                    "LEFT JOIN task_tags tt ON tt.tag_id = tg.id "
                    "WHERE tt.tag_id IS NULL"
                )
                commit()
                flash("Etiquetas no usadas eliminadas.", "ok")
            except Exception as e:
                rollback()
                flash(f"No se pudieron eliminar las etiquetas no usadas: {e}", "error")

            return redirect(url_for("admin"))
        
        if action == "purge_empty_projects":
            if not admin_required():
                flash("No autorizado.", "error")
                return redirect(url_for("admin"))

            try:
                exec_sql(
                    "UPDATE projects p "
                    "LEFT JOIN tasks t ON t.project_id = p.id AND t.deleted_at IS NULL "
                    "SET p.deleted_prev_archived=p.archived, p.deleted_at=NOW(), p.archived=1, p.archived_at=COALESCE(p.archived_at, NOW()), p.updated_at=NOW() "
                    "WHERE t.id IS NULL AND p.deleted_at IS NULL"
                )
                commit()
                flash("Proyectos vacíos enviados a papelera.", "ok")
            except Exception as e:
                rollback()
                flash(f"No se pudieron enviar a papelera los proyectos vacíos: {e}", "error")

            return redirect(url_for("admin"))

        handled_trash = _handle_trash_action(action, "admin")
        if handled_trash is not None:
            return handled_trash

        handled_calendar = _handle_calendar_conflicts_action(action, "admin")
        if handled_calendar is not None:
            return handled_calendar

        handled_archive = _handle_archive_action(action, "admin")
        if handled_archive is not None:
            return handled_archive
            
        if action == "purge_completed_tasks":
            if not admin_required():
                flash("No autorizado.", "error")
                return redirect(url_for("admin"))

            days_raw = (request.form.get("older_than_days") or "").strip()
            allowed_days = {"7", "15", "30", "365"}

            if days_raw not in allowed_days:
                flash("Valor de antigüedad inválido.", "error")
                return redirect(url_for("admin"))

            days = int(days_raw)
            periodic_names = ("periodica", "periódica", "periodicas", "periódicas")

            try:
                exec_sql(
                    "UPDATE tasks t "
                    "SET t.deleted_prev_archived=t.archived, t.deleted_at=NOW(), t.archived=1, t.archived_at=COALESCE(t.archived_at, NOW()) "
                    "WHERE t.completed_at IS NOT NULL "
                    "AND t.deleted_at IS NULL "
                    "AND t.completed_at < (NOW() - INTERVAL %s DAY) "
                    "AND (t.recurrence_rule IS NULL OR TRIM(t.recurrence_rule)='') "
                    "AND NOT EXISTS ("
                    "  SELECT 1 FROM task_tags tt "
                    "  JOIN tags tg ON tg.id=tt.tag_id "
                    "  WHERE tt.task_id=t.id "
                    "  AND LOWER(tg.name) IN (LOWER(%s), LOWER(%s), LOWER(%s), LOWER(%s))"
                    ")",
                    (days, *periodic_names),
                )

                exec_sql(
                    "UPDATE tasks t "
                    "SET t.calendar_sync_state='pending_delete', t.calendar_local_changed_at=NOW() "
                    "WHERE t.completed_at IS NOT NULL "
                    "AND t.deleted_at IS NOT NULL "
                    "AND t.google_event_id IS NOT NULL "
                    "AND t.completed_at < (NOW() - INTERVAL %s DAY) "
                    "AND (t.recurrence_rule IS NULL OR TRIM(t.recurrence_rule)='') "
                    "AND NOT EXISTS ("
                    "  SELECT 1 FROM task_tags tt "
                    "  JOIN tags tg ON tg.id=tt.tag_id "
                    "  WHERE tt.task_id=t.id "
                    "  AND LOWER(tg.name) IN (LOWER(%s), LOWER(%s), LOWER(%s), LOWER(%s))"
                    ")",
                    (days, *periodic_names),
                )

                commit()
                flash(f"Tareas realizadas con antigüedad superior a {days} días enviadas a papelera.", "ok")
            except Exception as e:
                rollback()
                flash(f"No se pudieron enviar a papelera las tareas realizadas: {e}", "error")

            return redirect(url_for("admin"))

        if action == "archive_completed_orphans":
            if not admin_required():
                flash("No autorizado.", "error")
                return redirect(url_for("admin"))
            periodic_names = ("periodica", "periódica", "periodicas", "periódicas")

            try:
                total_row = q1(
                    "SELECT COUNT(*) AS c "
                    "FROM tasks t "
                    "WHERE t.archived=0 "
                    "AND t.deleted_at IS NULL "
                    "AND t.project_id IS NULL "
                    "AND t.completed_at IS NOT NULL "
                    "AND t.completed_at < (NOW() - INTERVAL 7 DAY) "
                    "AND (t.recurrence_rule IS NULL OR TRIM(t.recurrence_rule)='') "
                    "AND NOT EXISTS ("
                    "  SELECT 1 FROM task_tags tt "
                    "  JOIN tags tg ON tg.id=tt.tag_id "
                    "  WHERE tt.task_id=t.id "
                    "  AND LOWER(tg.name) IN (LOWER(%s), LOWER(%s), LOWER(%s), LOWER(%s))"
                    ")",
                    periodic_names,
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
                        "    WHERE t.archived=0 "
                        "    AND t.deleted_at IS NULL "
                        "    AND t.project_id IS NULL "
                        "    AND t.completed_at IS NOT NULL "
                        "    AND t.completed_at < (NOW() - INTERVAL 7 DAY) "
                        "    AND (t.recurrence_rule IS NULL OR TRIM(t.recurrence_rule)='') "
                        "    AND NOT EXISTS ("
                        "      SELECT 1 FROM task_tags tt "
                        "      JOIN tags tg ON tg.id=tt.tag_id "
                        "      WHERE tt.task_id=t.id "
                        "      AND LOWER(tg.name) IN (LOWER(%s), LOWER(%s), LOWER(%s), LOWER(%s))"
                        "    )"
                        "  ) AS eligible"
                        ")",
                        periodic_names,
                    )
                commit()
                flash(f"{total_to_archive} tareas archivadas.", "ok")
            except Exception as e:
                rollback()
                flash(f"No se pudieron archivar las tareas: {e}", "error")

            return redirect(url_for("admin"))

        if action == "test_db":
            ok = False
            err = ""
            try:
                c = get_db_conn()
                with c.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
                c.close()
                ok = True
            except Exception as e:
                err = str(e)
            flash("Conexión OK." if ok else f"Falló la conexión: {err}", "ok" if ok else "error")
            return redirect(url_for("admin"))
            
            
        if action == "save_pagination":
            cfg = load_config()
            appcfg = cfg.setdefault("app", {})
            pag = appcfg.setdefault("pagination", {})

            def _int_field(name: str, default: int, minv: int, maxv: int) -> int:
                raw = (request.form.get(name) or "").strip()
                try:
                    v = int(raw)
                except ValueError:
                    return default
                if v < minv:
                    return minv
                if v > maxv:
                    return maxv
                return v

            pag["agenda_per_page"] = _int_field("agenda_per_page", pag.get("agenda_per_page", 50), 5, 500)
            pag["search_per_page"] = _int_field("search_per_page", pag.get("search_per_page", 50), 5, 500)
            pag["tags_per_page"] = _int_field("tags_per_page", pag.get("tags_per_page", 12), 5, 200)
            pag["tag_detail_per_page"] = _int_field("tag_detail_per_page", pag.get("tag_detail_per_page", 50), 5, 500)
            pag["folders_per_page"] = _int_field("folders_per_page", pag.get("folders_per_page", 10), 5, 200)
            pag["filters_per_page"] = _int_field("filters_per_page", pag.get("filters_per_page", 15), 5, 500)
            pag["projects_per_page"] = _int_field("projects_per_page", pag.get("projects_per_page", 15), 5, 500)
            pag["nextactions_per_page"] = _int_field("nextactions_per_page", pag.get("nextactions_per_page", 25), 5, 500)
            pag["periodic_history_per_page"] = _int_field("periodic_history_per_page", pag.get("periodic_history_per_page", 20), 5, 500)
            # Archivo: claves separadas para tareas y proyectos (manteniendo compatibilidad con archive_per_page).
            legacy_archive = _int_field("archive_per_page", pag.get("archive_per_page", 25), 5, 500)
            pag["archive_tasks_per_page"] = _int_field(
                "archive_tasks_per_page",
                pag.get("archive_tasks_per_page", legacy_archive),
                5,
                500,
            )
            pag["archive_projects_per_page"] = _int_field(
                "archive_projects_per_page",
                pag.get("archive_projects_per_page", legacy_archive),
                5,
                500,
            )

            save_config(cfg)
            flash("Paginación guardada.", "ok")
            return redirect(url_for("admin"))

    cfg = load_config()
    archive_orphans_preview = []
    trash_counts = {"tasks": 0, "projects": 0}
    archive_counts = {"tasks": 0, "projects": 0}
    calendar_sync_stats = {
        "pending_push": 0,
        "pending_delete": 0,
        "conflict": 0,
        "error": 0,
    }

    # Estado del token para la card (unificado)
    token_path = gmail_token_path()
    google_token_status = "unknown"
    google_token_expiry = None
    if token_path.exists():
        try:
            with open(token_path, "r", encoding="utf-8") as f:
                token_data = json.load(f)
                google_token_expiry = token_data.get("expiry")
        except Exception:
            google_token_expiry = None
        from google.oauth2.credentials import Credentials
        creds = Credentials.from_authorized_user_file(str(token_path), [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/calendar"
        ])
        if creds and creds.valid:
            google_token_status = "ok"
        elif creds and creds.expired and creds.refresh_token:
            google_token_status = "refreshing"
        else:
            google_token_status = "expired"

    if admin_required():
        periodic_names = ("periodica", "periódica", "periodicas", "periódicas")
        api_tokens = q(
            "SELECT id, device_name, token, created_at, last_used_at, active FROM api_tokens WHERE user_id=%s ORDER BY created_at DESC",
            (get_api_token_owner_user_id(),),
        )
        for token_row in api_tokens:
            token_row["token_prefix"] = (token_row.get("token") or "")[:6]

        archive_orphans_preview = q(
            "SELECT t.id, t.title, t.completed_at, f.name AS folder_name "
            "FROM tasks t "
            "LEFT JOIN folders f ON f.id=t.folder_id "
            "WHERE t.archived=0 "
            "AND t.deleted_at IS NULL "
            "AND t.project_id IS NULL "
            "AND t.completed_at IS NOT NULL "
            "AND (t.recurrence_rule IS NULL OR TRIM(t.recurrence_rule)='') "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM task_tags tt "
            "  JOIN tags tg ON tg.id=tt.tag_id "
            "  WHERE tt.task_id=t.id "
            "  AND LOWER(tg.name) IN (LOWER(%s), LOWER(%s), LOWER(%s), LOWER(%s))"
            ") "
            "AND t.completed_at < (NOW() - INTERVAL 7 DAY) "
            "ORDER BY t.completed_at ASC, t.id ASC "
            "LIMIT 200",
            periodic_names,
        )

        trash_counts = _load_trash_view_data(task_page=1, project_page=1)["trash_counts"]
        archive_counts_row = q1(
            "SELECT "
            "(SELECT COUNT(*) FROM tasks WHERE archived=1 AND deleted_at IS NULL) AS tasks, "
            "(SELECT COUNT(*) FROM projects WHERE archived=1 AND deleted_at IS NULL) AS projects"
        )
        if archive_counts_row:
            archive_counts = {
                "tasks": int(archive_counts_row.get("tasks") or 0),
                "projects": int(archive_counts_row.get("projects") or 0),
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

    calendar_sync_last_info = session.pop("calendar_sync_last_info", None)
    calendar_sync_last_level = session.pop("calendar_sync_last_level", "ok")
    google_oauth_auth_url = session.get("google_oauth_auth_url")

    return render_template(
        "admin.html",
        cfg=cfg,
        is_admin=admin_required(),
        api_tokens=api_tokens,
        env_pwd_set=env_pwd_set,
        backups=list_backups(),
        archive_task_backups=list_archive_task_backups(),
        archive_orphans_preview=archive_orphans_preview,
        trash_counts=trash_counts,
        archive_counts=archive_counts,
        calendar_sync_stats=calendar_sync_stats,
        calendar_sync_last_info=calendar_sync_last_info,
        calendar_sync_last_level=calendar_sync_last_level,
        google_token_status=google_token_status,
        google_token_expiry=google_token_expiry,
    )


@app.route("/admin/google_oauth/callback")
def admin_google_oauth_callback():
    """Callback automático de Google OAuth. Recibe code y state, intercambia por token."""
    error = (request.args.get("error") or "").strip()
    if error:
        flash(f"OAuth denegado o cancelado: {error}", "error")
        return redirect(url_for("admin"))

    try:
        expected_state = session.pop("google_oauth_state", None)
        code_verifier = session.pop("google_oauth_code_verifier", None)
        incoming_state = (request.args.get("state") or "").strip()

        if not expected_state or not incoming_state or expected_state != incoming_state:
            raise RuntimeError("Estado OAuth inválido o expirado (state mismatch)")

        redirect_uri = "https://raspvinxeira.mooo.com:9999/gtdApp/admin/google_oauth/callback"
        flow = _build_admin_google_flow(redirect_uri)
        if code_verifier:
            flow.code_verifier = str(code_verifier)

        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials

        token_path = gmail_token_path()
        token_path.write_text(creds.to_json(), encoding="utf-8")
        try:
            os.chmod(token_path, 0o660)
        except Exception:
            pass

        if not admin_required():
            session["is_admin"] = True

        flash("Token de Google renovado correctamente.", "ok")
        return redirect(url_for("admin"))
    except Exception as e:
        flash(f"No se pudo completar la renovación OAuth: {e}", "error")
        return redirect(url_for("admin"))


@app.route("/executeSQL", methods=["GET", "POST"])
def execute_sql_view():
    if not admin_required():
        flash("No autorizado. Inicia sesión como admin.", "error")
        return redirect(url_for("admin"))

    cfg = load_config()
    appcfg = cfg.setdefault("app", {})
    tools_cfg = appcfg.setdefault("tools", {})
    raw_saved = tools_cfg.get("saved_sql_queries", [])
    saved_queries: List[Dict[str, str]] = []
    if isinstance(raw_saved, list):
        for item in raw_saved:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            sql = str(item.get("sql") or "").strip()
            if name and sql:
                saved_queries.append({"name": name, "sql": sql})

    sql_query_input = ""
    sql_result_cols: List[str] = []
    sql_result_rows: List[Any] = []
    sql_result_truncated = False
    sql_error = ""
    selected_query_name = ""

    def _run_query(query_text: str) -> None:
        nonlocal sql_result_cols, sql_result_rows, sql_result_truncated, sql_error
        normalized = query_text.strip().rstrip(";").strip()
        lowered = normalized.lower()
        allowed_prefixes = ("select", "show", "describe", "desc", "explain")

        if not normalized:
            sql_error = "Debes escribir una consulta SQL."
            return

        if not lowered.startswith(allowed_prefixes):
            sql_error = "Solo se permiten consultas de lectura: SELECT, SHOW, DESCRIBE, DESC o EXPLAIN."
            return

        try:
            with db().cursor() as cur:
                cur.execute(normalized)
                if cur.description:
                    sql_result_cols = [str(col[0]) for col in cur.description]
                    fetched = cur.fetchmany(201)
                    sql_result_rows = fetched[:200]
                    sql_result_truncated = len(fetched) > 200
                else:
                    sql_result_cols = ["resultado"]
                    sql_result_rows = [{"resultado": "Consulta ejecutada sin conjunto de filas."}]
        except Exception as e:
            sql_error = str(e)

    if request.method == "POST":
        action = (request.form.get("action") or "run_sql").strip().lower()
        sql_query_input = (request.form.get("sql_query") or "").strip()
        selected_query_name = (request.form.get("saved_query_name") or "").strip()

        if action == "load_query":
            loaded = next((x for x in saved_queries if x["name"] == selected_query_name), None)
            if not loaded:
                sql_error = "Consulta guardada no encontrada."
            else:
                sql_query_input = loaded["sql"]

        elif action == "save_query":
            query_name = (request.form.get("query_name") or "").strip()
            if not query_name:
                sql_error = "Debes indicar un nombre para la consulta."
            elif not sql_query_input:
                sql_error = "No hay SQL para guardar."
            else:
                replaced = False
                for item in saved_queries:
                    if item["name"] == query_name:
                        item["sql"] = sql_query_input
                        replaced = True
                        break
                if not replaced:
                    saved_queries.append({"name": query_name, "sql": sql_query_input})

                tools_cfg["saved_sql_queries"] = saved_queries
                try:
                    save_config(cfg)
                    selected_query_name = query_name
                    flash("Consulta guardada.", "ok")
                except Exception as e:
                    import logging
                    logging.exception("Error guardando config.json en executeSQL")
                    sql_error = f"Error guardando configuración: {e}"

        elif action == "delete_query":
            if not selected_query_name:
                sql_error = "Selecciona una consulta para eliminar."
            else:
                before = len(saved_queries)
                saved_queries = [x for x in saved_queries if x["name"] != selected_query_name]
                if len(saved_queries) == before:
                    sql_error = "Consulta guardada no encontrada."
                else:
                    tools_cfg["saved_sql_queries"] = saved_queries
                    save_config(cfg)
                    selected_query_name = ""
                    flash("Consulta eliminada.", "ok")

        else:
            _run_query(sql_query_input)

    return render_template(
        "execute_sql.html",
        sql_query_input=sql_query_input,
        sql_result_cols=sql_result_cols,
        sql_result_rows=sql_result_rows,
        sql_result_truncated=sql_result_truncated,
        sql_error=sql_error,
        saved_queries=saved_queries,
        selected_query_name=selected_query_name,
    )


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

# ---------------- Errors ----------------

@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404, msg="No encontrado."), 404

@app.errorhandler(500)
def server_error(e):
    return render_template("error.html", code=500, msg="Error interno."), 500

# ---------------- Backups ----------------

def list_backups() -> List[str]:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    files = []
    for p in BACKUP_DIR.glob("*.sql"):
        if p.is_file():
            files.append(p.name)
    return sorted(files, reverse=True)


def list_archive_task_backups() -> List[str]:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    files = []
    for p in BACKUP_DIR.glob("archive_tasks_*.json"):
        if p.is_file():
            files.append(p.name)
    return sorted(files, reverse=True)

def run_mysqldump_to_file(filepath: Path) -> Tuple[bool, str]:
    cfg = load_config()
    dbcfg = cfg["db"]
    cmd = [
        "/usr/bin/mysqldump",
        f"--host={dbcfg['host']}",
        f"--port={dbcfg['port']}",
        f"--user={dbcfg['user']}",
        f"--password={dbcfg['password']}",
        "--default-character-set=utf8mb4",
        "--single-transaction",
        "--routines",
        "--triggers",
        "--events",
        "--add-drop-table",
        dbcfg["database"],
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        if res.returncode != 0:
            return False, res.stderr.strip() or "mysqldump falló"
        filepath.write_text(res.stdout, encoding="utf-8")
        return True, "OK"
    except Exception as e:
        return False, str(e)

def run_mysql_import_from_file(filepath: Path) -> Tuple[bool, str]:
    cfg = load_config()
    dbcfg = cfg["db"]
    cmd = [
        "/usr/bin/mysql",
        f"--host={dbcfg['host']}",
        f"--port={dbcfg['port']}",
        f"--user={dbcfg['user']}",
        f"--password={dbcfg['password']}",
        "--default-character-set=utf8mb4",
        dbcfg["database"],
    ]
    try:
        sql = filepath.read_text(encoding="utf-8")
        res = subprocess.run(cmd, input=sql, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        if res.returncode != 0:
            return False, res.stderr.strip() or "mysql import falló"
        return True, "OK"
    except Exception as e:
        return False, str(e)

# ---------------- Vaciar / Importar / Exportar ----------------

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
        "SELECT t.id, t.title, t.notes, t.due_date, t.completed_at, t.last_completed_at, "
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
    
    
def _parse_date_iso(s: str) -> Optional[date]:
    s = (s or "").strip()
    if not s:
        return None
    # ISO first
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        pass
    # allow dd-mm-YYYY
    try:
        return datetime.strptime(s, "%d-%m-%Y").date()
    except ValueError:
        raise ValueError(f"Fecha inválida: '{s}'")


def _parse_dt_iso(s: str) -> Optional[datetime]:
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"Datetime inválido: '{s}'")

def _split_tags_csv(s: str) -> list[str]:
    s = (s or "").strip()
    if not s:
        return []
    parts = [p.strip() for p in s.split(",")]
    return [p for p in parts if p]

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



from flask import request

@app.context_processor
def inject_sidebar_tree():
    try:
        # 1) Carpetas
        folders = q("SELECT id, parent_id, name FROM folders ORDER BY name")
        folders_by_parent = {}
        parent_of = {}
        children_of = {}

        for f in folders:
            pid = f.get("parent_id")
            folders_by_parent.setdefault(pid, []).append(f)
            parent_of[f["id"]] = pid
            children_of.setdefault(pid, []).append(f["id"])
            children_of.setdefault(f["id"], [])

        # 2) Proyectos activos
        projects = q(
            "SELECT id, name, folder_id "
            "FROM projects "
            "WHERE archived=0 "
            "ORDER BY name"
        )

        # Contador de tareas abiertas por proyecto
        rows = q(
            "SELECT project_id, COUNT(*) AS n "
            "FROM tasks "
            "WHERE completed_at IS NULL "
            "AND archived=0 "
            "AND deleted_at IS NULL "
            "AND project_id IS NOT NULL "
            "GROUP BY project_id"
        )
        project_task_counts = {int(r["project_id"]): int(r["n"]) for r in rows}

        projects_by_folder = {}
        for p in projects:
            fid = p.get("folder_id")
            if fid is None:
                continue
            projects_by_folder.setdefault(fid, []).append(p)

        # --- NUEVO: contador directo de tareas por carpeta ---
        # solo tareas asignadas directamente a carpeta (no a proyecto)
        folder_direct_rows = q(
            "SELECT folder_id, COUNT(*) AS n "
            "FROM tasks "
            "WHERE completed_at IS NULL "
            "AND archived=0 "
            "AND deleted_at IS NULL "
            "AND folder_id IS NOT NULL "
            "AND project_id IS NULL "
            "GROUP BY folder_id"
        )
        folder_direct_counts = {int(r["folder_id"]): int(r["n"]) for r in folder_direct_rows}

        # 3) Carpeta y proyecto actual
        current_folder_id = None
        current_project_id = None
        ep = request.endpoint or ""

        if ep == "folder_detail":
            fid = request.view_args.get("folder_id") if request.view_args else None
            try:
                current_folder_id = int(fid) if fid is not None else None
            except (TypeError, ValueError):
                current_folder_id = None

        elif ep == "project_detail":
            pid = request.view_args.get("project_id") if request.view_args else None
            try:
                pid_int = int(pid) if pid is not None else None
            except (TypeError, ValueError):
                pid_int = None

            if pid_int is not None:
                current_project_id = pid_int
                row = q1("SELECT folder_id FROM projects WHERE id=%s", (pid_int,))
                if row and row.get("folder_id") is not None:
                    try:
                        current_folder_id = int(row["folder_id"])
                    except (TypeError, ValueError):
                        current_folder_id = None

        open_folder_ids = set()
        if current_folder_id is not None:
            cur = current_folder_id
            guard = 0
            while cur is not None and guard < 100:
                open_folder_ids.add(cur)
                cur = parent_of.get(cur)
                guard += 1

        return {
            "folders_by_parent": folders_by_parent,
            "projects_by_folder": projects_by_folder,
            "project_task_counts": project_task_counts,
            "folder_task_counts": folder_direct_counts,   # <- nuevo
            "current_folder_id": current_folder_id,
            "current_project_id": current_project_id,
            "open_folder_ids": open_folder_ids,
        }

    except Exception:
        return {
            "folders_by_parent": {},
            "projects_by_folder": {},
            "project_task_counts": {},
            "folder_task_counts": {},   # <- nuevo
            "current_folder_id": None,
            "current_project_id": None,
            "open_folder_ids": set(),
        }
        
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

    try:
        ast = parse_filter_expression(expr)

        # Por defecto: solo tareas abiertas
        if not ast_contains_done(ast):
            ast = And(ast, Not(Term("IDENT", "done")))

        where_sql, params = compile_filter_to_sql(ast)
    except Exception as e:
        flash(f"Expresión inválida: {e}", "error")
        return redirect(url_for("filters_view"))

    total_row = q1(
        "SELECT COUNT(*) AS c "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "LEFT JOIN folders pf ON pf.id=p.folder_id "
        f"WHERE {where_sql} AND t.archived=0 AND t.deleted_at IS NULL AND (t.project_id IS NULL OR p.archived = 0)",
        tuple(params),
    )

    total = int(total_row["c"]) if total_row else 0
    pages = max(1, (total + per_page - 1) // per_page)

    sql = (
        "SELECT t.id, t.title, t.notes, t.due_date, t.completed_at, t.recurrence_rule, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "LEFT JOIN folders pf ON pf.id=p.folder_id "
        f"WHERE {where_sql} AND t.archived=0 AND t.deleted_at IS NULL AND (t.project_id IS NULL OR p.archived = 0) "
        "ORDER BY (t.completed_at IS NOT NULL) ASC, (t.due_date IS NULL) ASC, t.due_date ASC, t.id DESC "
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
    
@app.route("/review")
def review():


    # Proyectos en Seguimiento y subcarpetas
    projects_seguimiento = []
    seguimiento_folder = q1("SELECT id FROM folders WHERE name='Seguimiento'")
    if seguimiento_folder and seguimiento_folder.get("id") is not None:
        seguimiento_ids = get_folder_tree_ids(int(seguimiento_folder["id"]))
        if seguimiento_ids:
            placeholders = ",".join(["%s"] * len(seguimiento_ids))
            projects_seguimiento = q(
                f"SELECT p.id, p.name, p.description, p.archived, f.name AS folder_name "
                f"FROM projects p "
                f"LEFT JOIN folders f ON f.id=p.folder_id "
                f"WHERE p.archived=0 AND p.deleted_at IS NULL AND p.folder_id IN ({placeholders}) "
                f"ORDER BY p.name",
                tuple(seguimiento_ids),
            ) or []

    # 1) Inbox
    inbox = q(
        "SELECT t.id, t.title, t.notes, t.due_date, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "WHERE t.completed_at IS NULL "
        "AND t.archived = 0 "
        "AND t.deleted_at IS NULL "
        "AND t.project_id IS NULL "
        "AND t.folder_id IS NULL "
        "ORDER BY t.id DESC"
    ) or []

    inbox_projects = q(
        "SELECT p.id, p.name, p.description, p.archived, f.name AS folder_name, f.id AS folder_id "
        "FROM projects p "
        "LEFT JOIN folders f ON f.id=p.folder_id "
        "WHERE p.archived=0 "
        "AND p.deleted_at IS NULL "
        "AND p.folder_id IS NULL "
        "ORDER BY p.name"
    ) or []

    # 2) NextActions
    nextaction_exists = tag_exists("NextAction")

    sometime_review_folder_ids = set()
    sometime_folder_row = q1("SELECT id FROM folders WHERE LOWER(name)=LOWER(%s)", ("Sometime",))
    if sometime_folder_row and sometime_folder_row.get("id") is not None:
        sometime_review_folder_ids = get_folder_tree_ids(int(sometime_folder_row["id"]))

    nextaction_folder_clause = ""
    nextaction_folder_params: Tuple[Any, ...] = ()
    if sometime_review_folder_ids:
        placeholders = ",".join(["%s"] * len(sometime_review_folder_ids))
        nextaction_folder_clause = f"AND (t.folder_id IS NULL OR t.folder_id NOT IN ({placeholders})) "
        nextaction_folder_params = tuple(sorted(sometime_review_folder_ids))

    nextactions_open = q(
        "SELECT t.id, t.title, t.notes, t.due_date, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "JOIN task_tags tt ON tt.task_id=t.id "
        "JOIN tags tg ON tg.id=tt.tag_id "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "WHERE t.completed_at IS NULL "
        "AND t.deleted_at IS NULL "
        "AND tg.name=%s "
        "AND (t.due_date IS NULL OR t.due_date >= CURDATE()) "
        + nextaction_folder_clause +
        "AND (t.project_id IS NULL OR p.archived = 0) "
        "ORDER BY (t.due_date IS NULL) ASC, t.due_date ASC, t.id DESC",
        ("NextAction",) + nextaction_folder_params
    ) if nextaction_exists else []

    nextactions_overdue = q(
        "SELECT t.id, t.title, t.notes, t.due_date, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "JOIN task_tags tt ON tt.task_id=t.id "
        "JOIN tags tg ON tg.id=tt.tag_id "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "WHERE t.completed_at IS NULL "
        "AND t.deleted_at IS NULL "
        "AND tg.name=%s "
        "AND t.due_date IS NOT NULL "
        "AND t.due_date < CURDATE() "
        + nextaction_folder_clause +
        "AND (t.project_id IS NULL OR p.archived = 0) "
        "ORDER BY t.due_date ASC, t.id DESC",
        ("NextAction",) + nextaction_folder_params
    ) if nextaction_exists else []

    # 3) Agenda futura
    en_seguimiento_exists = tag_exists("EnSeguimiento")
    agenda_exists = tag_exists("agenda")

    upcoming_7 = q(
        "SELECT t.id, t.title, t.notes, t.due_date, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "JOIN task_tags tt ON tt.task_id=t.id "
        "JOIN tags tg ON tg.id=tt.tag_id "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "WHERE t.completed_at IS NULL "
        "AND t.deleted_at IS NULL "
        "AND tg.name=%s "
        "AND t.due_date IS NOT NULL "
        "AND t.due_date >= CURDATE() "
        "AND t.due_date <= DATE_ADD(CURDATE(), INTERVAL 7 DAY) "
        "AND (t.project_id IS NULL OR p.archived = 0) "
        "ORDER BY t.due_date ASC, t.id DESC",
        ("agenda",)
    ) if agenda_exists else []

    en_seguimiento_now = q(
        "SELECT t.id, t.title, t.notes, t.due_date, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "JOIN task_tags tt ON tt.task_id=t.id "
        "JOIN tags tg ON tg.id=tt.tag_id "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "WHERE t.completed_at IS NULL "
        "AND t.deleted_at IS NULL "
        "AND tg.name=%s "
        "AND t.due_date IS NOT NULL "
        "AND t.due_date <= CURDATE() "
        "AND (t.project_id IS NULL OR p.archived = 0) "
        "ORDER BY (t.due_date IS NULL) ASC, t.due_date ASC, t.id DESC",
        ("EnSeguimiento",)
    ) if en_seguimiento_exists else []

    en_seguimiento_next_15 = q(
        "SELECT t.id, t.title, t.notes, t.due_date, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "JOIN task_tags tt ON tt.task_id=t.id "
        "JOIN tags tg ON tg.id=tt.tag_id "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "WHERE t.completed_at IS NULL "
        "AND t.deleted_at IS NULL "
        "AND tg.name=%s "
        "AND t.due_date IS NOT NULL "
        "AND t.due_date > CURDATE() "
        "AND t.due_date <= DATE_ADD(CURDATE(), INTERVAL 15 DAY) "
        "AND (t.project_id IS NULL OR p.archived = 0) "
        "ORDER BY t.due_date ASC, t.id DESC",
        ("EnSeguimiento",)
    ) if en_seguimiento_exists else []

    # 4) Agenda pasada


    agenda_overdue = q(
        "SELECT t.id, t.title, t.due_date, t.notes, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "JOIN task_tags tt ON tt.task_id=t.id "
        "JOIN tags tg ON tg.id=tt.tag_id "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "WHERE t.completed_at IS NULL "
        "AND t.deleted_at IS NULL "
        "AND tg.name=%s "
        "AND t.due_date IS NOT NULL "
        "AND t.due_date < CURDATE() "
        "AND (t.project_id IS NULL OR p.archived = 0) "
        "ORDER BY t.due_date ASC, t.id DESC",
        ("agenda",)
    ) if agenda_exists else []

    # 5) EnEspera
    en_espera_exists = tag_exists("EnEspera")
    en_espera_folder_exists = folder_exists("EnEspera")

    # Excluir tareas de proyectos en Seguimiento o subcarpetas
    seguimiento_folder = q1("SELECT id FROM folders WHERE name='Seguimiento'")
    seguimiento_ids = []
    if seguimiento_folder and seguimiento_folder.get("id") is not None:
        seguimiento_ids = get_folder_tree_ids(int(seguimiento_folder["id"]))
    seguimiento_clause = ""
    seguimiento_params = ()
    if seguimiento_ids:
        placeholders = ",".join(["%s"] * len(seguimiento_ids))
        seguimiento_clause = (
            f"AND (t.folder_id IS NULL OR t.folder_id NOT IN ({placeholders})) "
            f"AND (p.folder_id IS NULL OR p.folder_id NOT IN ({placeholders})) "
        )
        seguimiento_params = tuple(seguimiento_ids) + tuple(seguimiento_ids)
    en_espera_tasks = q(
        "SELECT t.id, t.title, t.due_date, t.notes, t.completed_at, t.recurrence_rule, t.priority, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "JOIN task_tags tt ON tt.task_id=t.id "
        "JOIN tags tg ON tg.id=tt.tag_id "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "WHERE t.completed_at IS NULL "
        "AND t.deleted_at IS NULL "
        "AND tg.name=%s "
        + seguimiento_clause +
        "AND (t.project_id IS NULL OR p.archived = 0) "
        "ORDER BY (t.due_date IS NULL) ASC, t.due_date ASC, t.id DESC",
        ("EnEspera",) + seguimiento_params
    ) if en_espera_exists else []

    en_espera_projects = []
    if en_espera_folder_exists:
        en_espera_folder = q1(
            "SELECT id FROM folders WHERE LOWER(name)=LOWER(%s)",
            ("EnEspera",),
        )
        if en_espera_folder and en_espera_folder.get("id") is not None:
            en_espera_projects = q(
                "SELECT p.id, p.name, p.description, p.archived, f.name AS folder_name "
                "FROM projects p "
                "LEFT JOIN folders f ON f.id=p.folder_id "
                "WHERE p.archived=0 "
                "AND p.deleted_at IS NULL "
                "AND p.folder_id=%s "
                "ORDER BY p.name",
                (en_espera_folder["id"],),
            ) or []

    # 6) Proyectos (excluyendo Sometime y sus subcarpetas)

    excluded_folder_ids = set()

    sometime_folder = q1("SELECT id FROM folders WHERE name='Sometime'")
    if sometime_folder:
        excluded_folder_ids.update(get_folder_tree_ids(sometime_folder['id']))

    agenda_folder = q1("SELECT id FROM folders WHERE name='🗃️ Agenda'")
    if agenda_folder:
        excluded_folder_ids.update(get_folder_tree_ids(agenda_folder['id']))


    en_espera_folder = q1("SELECT id FROM folders WHERE name='EnEspera'")
    if en_espera_folder:
        excluded_folder_ids.update(get_folder_tree_ids(en_espera_folder['id']))

    seguimiento_folder = q1("SELECT id FROM folders WHERE name='Seguimiento'")
    if seguimiento_folder:
        excluded_folder_ids.update(get_folder_tree_ids(seguimiento_folder['id']))

    if excluded_folder_ids:
        ids_placeholder = ','.join(str(fid) for fid in excluded_folder_ids)
        active_projects = q(
            f"SELECT p.id, p.name, p.description, p.archived, p.folder_id, f.name AS folder_name "
            f"FROM projects p "
            f"LEFT JOIN folders f ON f.id=p.folder_id "
            f"WHERE p.archived=0 "
            f"AND p.deleted_at IS NULL "
            f"AND (p.folder_id IS NULL OR p.folder_id NOT IN ({ids_placeholder})) "
            f"ORDER BY p.name"
        ) or []
    else:
        active_projects = q(
            "SELECT p.id, p.name, p.description, p.archived, p.folder_id, f.name AS folder_name "
            "FROM projects p "
            "LEFT JOIN folders f ON f.id=p.folder_id "
            "WHERE p.archived=0 "
            "AND p.deleted_at IS NULL "
            "ORDER BY p.name"
        ) or []

    if excluded_folder_ids:
        ids_placeholder = ','.join(str(fid) for fid in excluded_folder_ids)
        empty_projects = q(
            f"SELECT p.id, p.name, p.description, p.archived, f.name AS folder_name "
            f"FROM projects p "
            f"LEFT JOIN folders f ON f.id=p.folder_id "
            f"LEFT JOIN tasks t ON t.project_id = p.id AND t.completed_at IS NULL AND t.archived=0 AND t.deleted_at IS NULL "
            f"WHERE p.archived = 0 "
            f"AND p.deleted_at IS NULL "
            f"AND (p.folder_id IS NULL OR p.folder_id NOT IN ({ids_placeholder})) "
            f"GROUP BY p.id, p.name, p.description, p.archived, f.name "
            f"HAVING COUNT(t.id) = 0 "
            f"ORDER BY p.name"
        ) or []
    else:
        empty_projects = q(
            "SELECT p.id, p.name, p.description, p.archived, f.name AS folder_name "
            "FROM projects p "
            "LEFT JOIN folders f ON f.id=p.folder_id "
            "LEFT JOIN tasks t ON t.project_id = p.id AND t.completed_at IS NULL AND t.archived=0 AND t.deleted_at IS NULL "
            "WHERE p.archived = 0 "
            "AND p.deleted_at IS NULL "
            "GROUP BY p.id, p.name, p.description, p.archived, f.name "
            "HAVING COUNT(t.id) = 0 "
            "ORDER BY p.name"
        ) or []

    rutinas_root = q1("SELECT id FROM folders WHERE name=%s", ("♲ Rutinas",))
    if not rutinas_root:
        rutinas_root = q1("SELECT id FROM folders WHERE LOWER(name)=LOWER(%s)", ("Rutinas",))

    rutinas_folder_ids = set()
    if rutinas_root and rutinas_root.get("id") is not None:
        rutinas_folder_ids = get_folder_tree_ids(int(rutinas_root["id"]))

    active_projects = [p for p in active_projects if p.get("folder_id") is not None]

    active_projects_rutinas = [
        p for p in active_projects
        if p.get("folder_id") is not None and int(p.get("folder_id")) in rutinas_folder_ids
    ]
    active_projects_other = [
        p for p in active_projects
        if p.get("folder_id") is not None and int(p.get("folder_id")) not in rutinas_folder_ids
    ]

    empty_projects = [p for p in empty_projects if p.get("folder_name")]

    # 7) ADTV / SomeTime
    sometime_folder_exists = folder_exists("Sometime")
    adtv_folder_exists = folder_exists("ADTV")
    esta_semana_no_folder_exists = folder_exists("🔜 EstaSemanaNo")

    sometime_tasks_no_project = []
    sometime_folder_row = q1("SELECT id FROM folders WHERE LOWER(name)=LOWER(%s)", ("Sometime",))
    if sometime_folder_row and sometime_folder_row.get("id") is not None:
        sometime_ids = get_folder_tree_ids(int(sometime_folder_row["id"]))
        if sometime_ids:
            placeholders = ",".join(["%s"] * len(sometime_ids))
            sometime_tasks_no_project = q(
                "SELECT t.id, t.title, t.notes, t.due_date, t.completed_at, t.recurrence_rule, t.priority, "
                "f.name AS folder_name, f.id AS folder_id "
                "FROM tasks t "
                "LEFT JOIN folders f ON f.id=t.folder_id "
                f"WHERE t.completed_at IS NULL AND t.deleted_at IS NULL AND t.project_id IS NULL AND t.folder_id IN ({placeholders}) "
                "ORDER BY (t.due_date IS NULL) ASC, t.due_date ASC, t.id DESC",
                tuple(sometime_ids),
            ) or []

    adtv_projects = q(
        "SELECT p.id, p.name, p.description, p.archived, f.name AS folder_name "
        "FROM projects p "
        "JOIN folders f ON f.id=p.folder_id "
        "WHERE p.archived=0 AND p.deleted_at IS NULL AND f.name=%s "
        "ORDER BY p.name",
        ("ADTV",)
    ) if adtv_folder_exists else []

    esta_semana_no_projects = q(
        "SELECT p.id, p.name, p.description, p.archived, f.name AS folder_name "
        "FROM projects p "
        "JOIN folders f ON f.id=p.folder_id "
        "WHERE p.archived=0 AND p.deleted_at IS NULL AND f.name=%s "
        "ORDER BY p.name",
        ("🔜 EstaSemanaNo",)
    ) if esta_semana_no_folder_exists else []

    # 8) Checklists
    checklists_folder_exists = folder_exists("✅ Checklists")
    checklist_tasks = []
    checklist_projects = []

    if checklists_folder_exists:
        checklists_folder = q1("SELECT id FROM folders WHERE name='✅ Checklists'")
        if checklists_folder:
            checklists_folder_id = checklists_folder['id']
            # Tareas directas en la carpeta (sin proyecto)
            checklist_tasks = q(
                "SELECT t.id, t.title, t.notes, t.due_date, t.completed_at, t.recurrence_rule, t.priority, "
                "p.name AS project_name, p.id AS project_id, "
                "f.name AS folder_name, f.id AS folder_id "
                "FROM tasks t "
                "LEFT JOIN projects p ON p.id=t.project_id "
                "LEFT JOIN folders f ON f.id=t.folder_id "
                "WHERE t.completed_at IS NULL "
                "AND t.deleted_at IS NULL "
                "AND t.folder_id=%s "
                "AND t.project_id IS NULL "
                "ORDER BY t.id DESC",
                (checklists_folder_id,)
            ) or []
            # Proyectos en la carpeta
            checklist_projects = q(
                "SELECT p.id, p.name, p.description, p.archived, f.name AS folder_name "
                "FROM projects p "
                "LEFT JOIN folders f ON f.id=p.folder_id "
                "WHERE p.archived=0 "
                "AND p.deleted_at IS NULL "
                "AND p.folder_id=%s "
                "ORDER BY p.name",
                (checklists_folder_id,)
            ) or []

    # Tags map de todas las listas de tareas
    all_task_ids = []
    for group in (
        inbox,
        nextactions_open,
        nextactions_overdue,
        upcoming_7,
        en_seguimiento_now,
        en_seguimiento_next_15,
        agenda_overdue,
        en_espera_tasks,
        sometime_tasks_no_project,
        checklist_tasks,
    ):
        all_task_ids.extend([t["id"] for t in group])

    tags_map = load_tags_map(all_task_ids) if all_task_ids else {}

    return render_template(
        "review.html",
        inbox=inbox,
        inbox_projects=inbox_projects,
        nextaction_exists=nextaction_exists,
        nextactions_open=nextactions_open,
        nextactions_overdue=nextactions_overdue,
        en_seguimiento_exists=en_seguimiento_exists,
        upcoming_7=upcoming_7,
        en_seguimiento_now=en_seguimiento_now,
        en_seguimiento_next_15=en_seguimiento_next_15,
        agenda_exists=agenda_exists,
        agenda_overdue=agenda_overdue,
        en_espera_exists=en_espera_exists,
        en_espera_tasks=en_espera_tasks,
        en_espera_folder_exists=en_espera_folder_exists,
        en_espera_projects=en_espera_projects,
        active_projects=active_projects,
        active_projects_rutinas=active_projects_rutinas,
        active_projects_other=active_projects_other,
        empty_projects=empty_projects,
        sometime_folder_exists=sometime_folder_exists,
        sometime_tasks_no_project=sometime_tasks_no_project,
        adtv_folder_exists=adtv_folder_exists,
        adtv_projects=adtv_projects,
        esta_semana_no_folder_exists=esta_semana_no_folder_exists,
        esta_semana_no_projects=esta_semana_no_projects,
        checklists_folder_exists=checklists_folder_exists,
        checklist_tasks=checklist_tasks,
        checklist_projects=checklist_projects,
        tags_map=tags_map,
        projects_seguimiento=projects_seguimiento,
    )
    
    
@app.route("/next")
def next_actions():
    per_page = cfg_int(["app", "pagination", "nextactions_per_page"], default=25, min_v=5, max_v=500)

    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    page = max(page, 1)

    tag = q1("SELECT id FROM tags WHERE name='NextAction'")

    if not tag:
        rows = []
        total = 0
        pages = 1
    else:
        total_row = q1(
            "SELECT COUNT(*) AS c "
            "FROM tasks t "
            "JOIN task_tags tt ON tt.task_id=t.id "
            "LEFT JOIN projects p ON p.id=t.project_id "
            "WHERE tt.tag_id=%s "
            "AND t.completed_at IS NULL "
            "AND t.deleted_at IS NULL "
            "AND (t.project_id IS NULL OR p.archived = 0)",
            (tag["id"],)
        )
        total = int(total_row["c"]) if total_row else 0
        pages = max(1, (total + per_page - 1) // per_page)

        if page > pages:
            page = pages

        offset = (page - 1) * per_page

        rows = q(
            "SELECT t.id, t.title, t.notes, t.due_date, t.due_time, t.recurrence_rule, t.completed_at, t.priority, "
            "p.name AS project_name, p.id AS project_id, "
            "fd.name AS folder_name, fd.id AS folder_id "
            "FROM tasks t "
            "JOIN task_tags tt ON tt.task_id=t.id "
            "LEFT JOIN projects p ON p.id=t.project_id "
            "LEFT JOIN folders fd ON fd.id = COALESCE(t.folder_id, p.folder_id) "
            "WHERE tt.tag_id=%s "
            "AND t.completed_at IS NULL "
            "AND t.deleted_at IS NULL "
            "AND (t.project_id IS NULL OR p.archived = 0) "
            "ORDER BY (t.due_date IS NULL) ASC, t.due_date ASC, t.id DESC "
            "LIMIT %s OFFSET %s",
            (tag["id"], per_page, offset)
        )

    task_ids = [r["id"] for r in rows]
    tags_map = load_tags_map(task_ids) if task_ids else {}
    sub_counts = load_subtask_counts(subdb, task_ids)
    sub_map = load_subtasks_map(subdb, task_ids)

    return render_template(
        "next.html",
        rows=rows,
        tags_map=tags_map,
        sub_counts=sub_counts,
        sub_map=sub_map,
        page=page,
        pages=pages,
        total=total,
        per_page=per_page,
    )


def _ext_parse_scope(scope_raw: str) -> Tuple[str, date, date]:
    scope = (scope_raw or "today").strip().lower()
    today_d = _today_madrid()

    if scope == "today":
        return scope, today_d, today_d

    if scope == "week":
        monday_d = today_d - timedelta(days=today_d.weekday())
        sunday_d = monday_d + timedelta(days=6)
        return scope, monday_d, sunday_d

    if scope in ("next7", "7days"):
        end_d = today_d + timedelta(days=6)
        return "next7", today_d, end_d

    raise ValueError("Scope inválido")


def _to_hhmm(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "strftime"):
        return value.strftime("%H:%M")
    txt = str(value).strip()
    return txt[:5] if len(txt) >= 5 else None


@app.route("/api/extension/tasks")
@require_api_token
def api_extension_tasks():
    scope_raw = (request.args.get("scope") or "today").strip().lower()

    # Vista NextAction: tareas con etiqueta @NextAction (sin filtro de fecha)
    if scope_raw == "next":
        next_rows = q(
            "SELECT t.id, t.title, t.due_date, t.due_time, t.priority, t.completed_at, "
            "p.id AS project_id, p.name AS project_name, "
            "fd.id AS folder_id, fd.name AS folder_name "
            "FROM tasks t "
            "JOIN task_tags tt ON tt.task_id = t.id "
            "JOIN tags tg ON tg.id = tt.tag_id "
            "LEFT JOIN projects p ON p.id = t.project_id "
            "LEFT JOIN folders fd ON fd.id = COALESCE(t.folder_id, p.folder_id) "
            "WHERE LOWER(tg.name) = %s "
            "AND t.completed_at IS NULL "
            "AND t.archived = 0 "
            "AND t.deleted_at IS NULL "
            "AND (t.project_id IS NULL OR p.archived = 0) "
            "ORDER BY (t.priority IS NULL) ASC, t.priority ASC, "
            "(t.due_date IS NULL) ASC, t.due_date ASC, t.id DESC",
            ("nextaction",),
        )
        next_items = []
        for r in next_rows:
            next_items.append({
                "id": int(r["id"]),
                "title": r.get("title") or "",
                "priority": int(r["priority"]) if r.get("priority") in (1, 2, 3) else None,
                "due_date": r["due_date"].isoformat() if r.get("due_date") else None,
                "due_time": _to_hhmm(r.get("due_time")),
                "project_id": int(r["project_id"]) if r.get("project_id") else None,
                "project_name": r.get("project_name") or None,
                "folder_id": int(r["folder_id"]) if r.get("folder_id") else None,
                "folder_name": r.get("folder_name") or None,
                "completed": bool(r.get("completed_at")),
            })
        return jsonify({"ok": True, "scope": "next", "from": None, "to": None, "items": next_items})

    try:
        scope, start_d, end_d = _ext_parse_scope(scope_raw)
    except ValueError:
        return jsonify({"ok": False, "error": "scope inválido. Usa today, week, next7 o next"}), 400

    rows = q(
        "SELECT t.id, t.title, t.due_date, t.due_time, t.priority, t.completed_at, "
        "p.id AS project_id, p.name AS project_name, "
        "fd.id AS folder_id, fd.name AS folder_name "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id = COALESCE(t.folder_id, p.folder_id) "
        "WHERE t.completed_at IS NULL "
        "AND t.archived=0 "
        "AND t.deleted_at IS NULL "
        "AND t.due_date IS NOT NULL "
        "AND t.due_date >= %s "
        "AND t.due_date <= %s "
        "AND (t.project_id IS NULL OR p.archived = 0) "
        "ORDER BY t.due_date ASC, (t.due_time IS NULL) ASC, t.due_time ASC, t.id DESC",
        (start_d, end_d),
    )

    items = []
    for r in rows:
        items.append(
            {
                "id": int(r["id"]),
                "title": r.get("title") or "",
                "priority": int(r["priority"]) if r.get("priority") in (1, 2, 3) else None,
                "due_date": r["due_date"].isoformat() if r.get("due_date") else None,
                "due_time": _to_hhmm(r.get("due_time")),
                "project_id": int(r["project_id"]) if r.get("project_id") else None,
                "project_name": r.get("project_name") or None,
                "folder_id": int(r["folder_id"]) if r.get("folder_id") else None,
                "folder_name": r.get("folder_name") or None,
                "completed": bool(r.get("completed_at")),
            }
        )

    return jsonify(
        {
            "ok": True,
            "scope": scope,
            "from": start_d.isoformat(),
            "to": end_d.isoformat(),
            "items": items,
        }
    )


@app.route("/api/extension/tasks/<int:task_id>/toggle", methods=["POST"])
@require_api_token
def api_extension_task_toggle(task_id: int):
    task = q1(
        "SELECT id, completed_at, due_date, recurrence_rule, project_id, archived "
        "FROM tasks WHERE id=%s",
        (task_id,),
    )
    if not task:
        return jsonify({"ok": False, "error": "Tarea no encontrada"}), 404

    now = datetime.now(ZoneInfo("Europe/Madrid")).replace(tzinfo=None)

    try:
        if task["completed_at"]:
            exec_sql("UPDATE tasks SET completed_at=NULL WHERE id=%s", (task_id,))
            completed = False
        else:
            if task.get("recurrence_rule") and task.get("due_date"):
                rule = parse_rrule(task["recurrence_rule"])
                previous_due = task["due_date"]
                next_due = next_due_date(task["due_date"], rule)
                today_d = now.date()

                if next_due < today_d:
                    next_due = next_due_date_after_today(task["due_date"], rule, today_d)

                exec_sql(
                    "UPDATE tasks SET last_completed_at=%s, due_date=%s, completed_at=NULL WHERE id=%s",
                    (now, next_due, task_id),
                )
                exec_sql(
                    "INSERT INTO recurring_task_runs(task_id, executed_at, previous_due_date, next_due_date) "
                    "VALUES(%s, %s, %s, %s)",
                    (task_id, now, previous_due, next_due),
                )
                exec_sql(
                    "UPDATE subtasks SET completed_at=NULL WHERE task_id=%s",
                    (task_id,),
                )
                completed = False
            else:
                has_nextaction = q1(
                    "SELECT 1 AS ok "
                    "FROM task_tags tt "
                    "JOIN tags tg ON tg.id=tt.tag_id "
                    "WHERE tt.task_id=%s AND LOWER(tg.name)=LOWER(%s) "
                    "LIMIT 1",
                    (task_id, "NextAction"),
                ) is not None

                if has_nextaction:
                    exec_sql(
                        "DELETE tt FROM task_tags tt "
                        "JOIN tags tg ON tg.id=tt.tag_id "
                        "WHERE tt.task_id=%s AND LOWER(tg.name)=LOWER(%s)",
                        (task_id, "NextAction"),
                    )

                exec_sql("UPDATE tasks SET completed_at=%s WHERE id=%s", (now, task_id))
                completed = True

                promote_nextaction = cfg_bool(
                    ["app", "behavior", "promote_nextaction_on_complete"],
                    default=True,
                )
                # Override a nivel de proyecto (si está definido) tiene prioridad sobre el global
                if has_nextaction and task.get("project_id"):
                    _proj_promo = q1(
                        "SELECT auto_promote_nextaction FROM projects WHERE id=%s",
                        (task["project_id"],),
                    )
                    if _proj_promo and _proj_promo.get("auto_promote_nextaction") is not None:
                        promote_nextaction = bool(_proj_promo["auto_promote_nextaction"])

                if promote_nextaction and has_nextaction and task.get("project_id"):
                    next_task = q1(
                        "SELECT id "
                        "FROM tasks "
                        "WHERE project_id=%s "
                        "AND archived=0 "
                        "AND completed_at IS NULL "
                        "AND id<>%s "
                        "ORDER BY (sort_order IS NULL) ASC, sort_order ASC, (due_date IS NULL) ASC, due_date ASC, id ASC "
                        "LIMIT 1",
                        (task["project_id"], task_id),
                    )
                    if next_task:
                        next_has_nextaction = q1(
                            "SELECT 1 AS ok "
                            "FROM task_tags tt "
                            "JOIN tags tg ON tg.id=tt.tag_id "
                            "WHERE tt.task_id=%s AND LOWER(tg.name)=LOWER(%s) "
                            "LIMIT 1",
                            (next_task["id"], "NextAction"),
                        ) is not None
                        if not next_has_nextaction:
                            next_tag = q1(
                                "SELECT id FROM tags WHERE LOWER(name)=LOWER(%s) ORDER BY id ASC LIMIT 1",
                                ("NextAction",),
                            )
                            next_tag_id = int(next_tag["id"]) if next_tag else get_or_create_tag("NextAction")
                            exec_sql(
                                "INSERT IGNORE INTO task_tags(task_id, tag_id) VALUES(%s,%s)",
                                (next_task["id"], next_tag_id),
                            )

        _mark_task_calendar_dirty(task_id)
        commit()
        return jsonify({"ok": True, "task_id": task_id, "completed": completed})
    except Exception as e:
        rollback()
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/extension/tasks/add_page", methods=["POST"])
@require_api_token
def api_extension_add_page_task():
    payload = request.get_json(silent=True) or {}
    page_url = (payload.get("url") or "").strip()
    page_title = (payload.get("page_title") or "").strip()
    replacement_text = normalize_name(payload.get("replacement_text") or "")
    priority = coerce_priority(payload.get("priority"), default=None)

    parsed = urlparse(page_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return jsonify({"ok": False, "error": "URL inválida"}), 400

    # Si hay texto de reemplazo, se guarda como título y el enlace en notas.
    # Si no, el título es el enlace completo para mantenerlo visible en la lista.
    task_title = replacement_text or page_url
    notes = page_url if replacement_text else (page_title or None)

    try:
        task_id = exec_sql(
            "INSERT INTO tasks(title, notes, project_id, folder_id, due_date, due_time, recurrence_rule, priority) "
            "VALUES(%s,%s,NULL,NULL,NULL,NULL,NULL,%s)",
            (task_title, notes, priority),
        )
        _mark_task_calendar_dirty(task_id, force_push_if_empty=True)
        commit()
        return jsonify(
            {
                "ok": True,
                "task": {
                    "id": int(task_id),
                    "title": task_title,
                    "notes": notes,
                    "project_id": None,
                    "project_name": None,
                },
            }
        )
    except Exception as e:
        rollback()
        return jsonify({"ok": False, "error": str(e)}), 500
    
  
@app.route("/api/tags/search")
def api_tags_search():
    qtxt = (request.args.get("q") or "").strip().lower()

    if not qtxt:
        return jsonify({"items": []})

    qtxt = qtxt[:50]

    rows = q(
        "SELECT id, name "
        "FROM tags "
        "WHERE LOWER(name) LIKE %s "
        "ORDER BY name "
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

    try:
        if target_type == "project":
            project = q1(
                "SELECT id FROM projects WHERE id=%s AND archived=0 AND deleted_at IS NULL",
                (target_id,),
            )
            if not project:
                return jsonify({"ok": False, "error": "Proyecto destino no válido"}), 400
            exec_sql(
                "UPDATE tasks SET project_id=%s, folder_id=NULL, sort_order=%s WHERE id=%s",
                (target_id, next_project_task_sort_order(target_id), task_id),
            )
            commit()
            return jsonify({"ok": True, "message": "Tarea movida a proyecto"})

        if target_type == "folder":
            folder = q1("SELECT id FROM folders WHERE id=%s", (target_id,))
            if not folder:
                return jsonify({"ok": False, "error": "Carpeta destino no válida"}), 400
            exec_sql("UPDATE tasks SET folder_id=%s, project_id=NULL WHERE id=%s", (target_id, task_id))
            commit()
            return jsonify({"ok": True, "message": "Tarea movida a carpeta"})

        return jsonify({"ok": False, "error": "Tipo de destino no soportado"}), 400
    except Exception as e:
        rollback()
        return jsonify({"ok": False, "error": str(e)}), 500


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


@app.route("/api/tasks/<int:task_id>/toggle_preview")
def api_task_toggle_preview(task_id: int):
    task = q1(
        "SELECT id, title, completed_at, due_date, recurrence_rule "
        "FROM tasks WHERE id=%s",
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

    # Usar siempre due_date como base, igual que en task_toggle
    base_date = task["due_date"]
    keep_due = next_due_date(base_date, rule)

    # Si la fecha base es hoy, proponemos hoy como válida
    if base_date == today_d:
        return jsonify({"requires_choice": False})

    # Si la próxima fecha calculada es hoy o mayor, no hay conflicto
    if keep_due >= today_d:
        return jsonify({"requires_choice": False})

    # Si la fecha es menor que hoy, buscar la siguiente futura
    future_due = next_due_date_after_today(base_date, rule, today_d)
    return jsonify(
        {
            "requires_choice": True,
            "task_id": int(task["id"]),
            "title": task.get("title") or "",
            "today": today_d.isoformat(),
            "keep_due": keep_due.isoformat(),
            "future_due": future_due.isoformat(),
        }
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
            tags = re.findall(r'@([^\s@#]+)', raw_work)

            detected_due_date = None
            detected_due_time = None
            if not payload.get("due_date"):
                detected_due_date, raw_work = extract_due_date_from_quick(raw_work)
            detected_due_time, raw_work = extract_due_time_from_quick(raw_work)

            recurrence = None
            for pattern, rule in RECURRENCE_PATTERNS.items():
                if re.search(pattern, raw_work, flags=re.IGNORECASE):
                    recurrence = rule
                    break

            project_name = None
            project_candidates = re.findall(r'#([^\s#]+)', raw_work)
            for candidate in project_candidates:
                if re.fullmatch(r'\d{2}-\d{2}-\d{4}', candidate):
                    continue
                project_name = normalize_name(candidate)
                break

            title = raw_work
            title = re.sub(r'@([^\s@#]+)', '', title)
            title = re.sub(r'\bcada\s+dia\b', '', title, flags=re.IGNORECASE)
            title = re.sub(r'\bcada\s+semana\b', '', title, flags=re.IGNORECASE)
            title = re.sub(r'\bcada\s+mes\b', '', title, flags=re.IGNORECASE)
            title = re.sub(r'\bcada\s+año\b', '', title, flags=re.IGNORECASE)
            title = re.sub(r'#([^\s#]+)', '', title)
            title = re.sub(TIME_TOKEN_RE, '', title)
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
        tag_agenda_id = get_or_create_tag("agenda")

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