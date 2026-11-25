"""Utilidades sin estado: conversión de valores, fechas, paginación, URLs seguras."""
from __future__ import annotations

import calendar
import re
import unicodedata
from datetime import date, datetime, time, timedelta
from typing import Any, Optional, Tuple
from urllib.parse import quote_plus, urlparse
from zoneinfo import ZoneInfo

from flask import request, url_for


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


def location_to_href(location: Optional[str]) -> Optional[str]:
    raw = (location or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme in {"http", "https"}:
        return raw
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(raw)}"


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


def normalize_name(s: str) -> str:
    return (s or "").strip()


def normalize_tag_key(tag_name: str) -> str:
    raw = (tag_name or "").strip().lower().lstrip("@")
    normalized = unicodedata.normalize("NFD", raw)
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def _today_madrid() -> date:
    return datetime.now(ZoneInfo("Europe/Madrid")).date()


def _add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


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


def format_hhmm(value: Any) -> str:
    """Hora como "HH:MM" (filtro Jinja ``hhmm``); "" si no hay hora o no es válida.

    pymysql devuelve las columnas TIME como ``timedelta`` y ``str()`` no rellena con
    ceros ("9:15:00"), así que recortar ``[:5]`` daba "9:15:" (inválido para <input type="time">).
    """
    if value is None or value == "":
        return ""
    if isinstance(value, (time, datetime)):
        return value.strftime("%H:%M")
    if isinstance(value, timedelta):
        minutes = int(value.total_seconds()) // 60
        return f"{minutes // 60 % 24:02d}:{minutes % 60:02d}"
    match = re.match(r"^\s*(\d{1,2}):(\d{2})", str(value))
    if not match:
        return ""
    return f"{int(match.group(1)):02d}:{match.group(2)}"
