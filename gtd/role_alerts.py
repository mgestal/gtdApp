"""Avisos automáticos por etiqueta GTD (EnSeguimiento, Agenda) al llegar la fecha de la tarea.

Lo ejecuta ``send_reminders.py`` (cada 15 minutos) tras los recordatorios normales.
No crea recordatorios en las tareas: para cada rol activado busca las tareas abiertas con
su etiqueta (o un alias) cuya fecha (efectiva, para las periódicas) está próxima, y avisa
10 minutos antes de la hora de la tarea o, si no tiene hora, a la hora configurada para ese rol.

Canales por rol: Telegram, navegador (push a los dispositivos activos) y app (campana).
Cada envío se registra por (rol, tarea, momento programado, destino) en
``role_alert_deliveries``: no se repite, los fallos se reintentan hasta 24 h después del
momento programado y, si la fecha cambia, se avisa en la nueva. La fila con destino
``app`` es la entrada de la campana (``read_at_utc`` = leída).

Configuración: ``config.json`` → ``app.notifications.<rol>``
(``enabled``, ``time`` "HH:MM", ``telegram``, ``browser``, ``app``).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

import reminders
from browser_push import WebPushException, push_usable_sql, send_browser_push
from py_vapid import VapidException
from requests.exceptions import RequestException

from gtd.config import load_config
from gtd.db import commit, exec_sql, q, q1, rollback
from gtd.roles import role_tag, role_tag_filter
from gtd.recurrence import recurring_effective_due_sql

# Roles con aviso automático: (rol, emoji, activado por defecto)
ALERT_ROLES = [
    ("following", "👀", True),
    ("agenda", "📅", False),
]
ROLE_KEYS = [role for role, _emoji, _enabled in ALERT_ROLES]
CHANNELS = ("telegram", "browser", "app")
RETRY_WINDOW = timedelta(hours=24)
TIMED_TASK_LEAD = timedelta(minutes=10)  # antelación para tareas con hora
WORKER_INTERVAL_MINUTES = 15  # send_reminders.py se ejecuta cada 15 min (:00, :15, :30, :45)
LOCK_NAME = "gtdapp_role_alerts"


def _emoji(role: str) -> str:
    return next(emoji for r, emoji, _ in ALERT_ROLES if r == role)


def role_alert_settings(role: str, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Ajustes del rol con valores por defecto; ``time`` se devuelve como ``datetime.time``."""
    cfg = cfg if cfg is not None else load_config()
    default_enabled = next(enabled for r, _e, enabled in ALERT_ROLES if r == role)
    settings: Dict[str, Any] = {"enabled": default_enabled, "time": "09:00",
                                "telegram": True, "browser": True, "app": True}
    raw = ((cfg.get("app") or {}).get("notifications") or {}).get(role) or {}
    if isinstance(raw, dict):
        settings.update({k: raw[k] for k in settings if k in raw})
    try:
        settings["time"] = datetime.strptime(str(settings["time"]), "%H:%M").time()
    except ValueError:
        settings["time"] = time(9, 0)
    for key in ("enabled",) + CHANNELS:
        settings[key] = bool(settings[key])
    return settings


def all_role_alert_settings(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, Any]]:
    cfg = cfg if cfg is not None else load_config()
    return {role: role_alert_settings(role, cfg) for role in ROLE_KEYS}


def scheduled_for(due_date: date, due_time: Any, default_time: time, tz: ZoneInfo) -> datetime:
    """Momento UTC del aviso.

    Sin hora: el día de la fecha a ``default_time``. Con hora: 10 min antes, adelantado al
    inicio del tramo de 15 min en que cae (el de la pasada del proceso), para que el aviso
    llegue antes de la tarea aunque el proceso solo se ejecute cada 15 min.
    """
    task_time = reminders.db_time(due_time)
    if task_time is None:
        return reminders.local_to_utc(datetime.combine(due_date, default_time), tz)
    at = reminders.local_to_utc(datetime.combine(due_date, task_time), tz) - TIMED_TASK_LEAD
    return at.replace(minute=at.minute - at.minute % WORKER_INTERVAL_MINUTES, second=0, microsecond=0)


def _candidates(role: str, today: date) -> List[Dict[str, Any]]:
    recurring_join, effective_due = recurring_effective_due_sql("t")
    tag_clause, tag_params = role_tag_filter(role)
    return q(
        f"SELECT t.id, t.title, {effective_due} AS due_date, t.due_time "
        "FROM tasks t LEFT JOIN projects p ON p.id=t.project_id "
        f"{recurring_join}"
        "WHERE t.completed_at IS NULL AND t.archived=0 AND t.deleted_at IS NULL "
        "AND (t.project_id IS NULL OR (p.archived=0 AND p.deleted_at IS NULL)) "
        + tag_clause +
        f"AND {effective_due} BETWEEN %s AND %s ORDER BY t.id",
        # mañana: una tarea a las 00:05 avisa hoy a las 23:55; ayer: reintentos dentro de 24 h
        tuple(tag_params) + (today - timedelta(days=1), today + timedelta(days=1)),
    ) or []


def _delivery_status(role: str, task_id: int, scheduled: datetime, target: str) -> Optional[str]:
    row = q1("SELECT status FROM role_alert_deliveries "
             "WHERE role=%s AND task_id=%s AND scheduled_at_utc=%s AND target=%s",
             (role, task_id, scheduled, target))
    return row["status"] if row else None


def _record(role: str, task_id: int, scheduled: datetime, target: str, status: str,
            error: Optional[str] = None) -> None:
    exec_sql(
        "INSERT INTO role_alert_deliveries"
        "(role, task_id, scheduled_at_utc, target, status, attempts, updated_at_utc, last_error) "
        "VALUES(%s,%s,%s,%s,%s,1,%s,%s) ON DUPLICATE KEY UPDATE status=VALUES(status), "
        "attempts=attempts+1, updated_at_utc=VALUES(updated_at_utc), last_error=VALUES(last_error)",
        (role, task_id, scheduled, target, status, reminders.utc_now(), error),
    )
    commit()


def _message(role: str, task: Dict[str, Any], public_url: str) -> str:
    text = f"{_emoji(role)} {role_tag(role)}: llega la fecha\n\n{task['title']}"
    text += f"\nFecha: {task['due_date'].strftime('%d/%m/%Y')}"
    due_time = reminders.db_time(task.get("due_time"))
    if due_time:
        text += f" a las {due_time.strftime('%H:%M')}"
    if public_url:
        text += f"\n\n{public_url.rstrip('/')}/tasks/{task['id']}/edit"
    return text


def run_role_alerts(tz: ZoneInfo, start: time, end: time, token_loader: Callable[[], str],
                    chat_loader: Callable[[], int], public_url: str = "",
                    cfg: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
    """Una pasada de avisos para todos los roles. Devuelve {sent, failed}; requiere contexto de app."""
    counts = {"sent": 0, "failed": 0}
    cfg = cfg if cfg is not None else load_config()
    active = {role: s for role, s in all_role_alert_settings(cfg).items()
              if s["enabled"] and any(s[c] for c in CHANNELS)}
    if not active:
        return counts
    now = reminders.utc_now()
    if not reminders.within_notification_hours(now, tz, start, end):
        return counts
    lock = q1("SELECT GET_LOCK(%s, 0) AS ok", (LOCK_NAME,))
    if not lock or lock.get("ok") != 1:
        return counts
    try:
        today = now.replace(tzinfo=timezone.utc).astimezone(tz).date()
        subscriptions = q("SELECT * FROM push_subscriptions WHERE " + push_usable_sql() + " ORDER BY id") \
            if any(s["browser"] for s in active.values()) else []
        for role, settings in active.items():
            for task in _candidates(role, today):
                try:
                    scheduled = scheduled_for(task["due_date"], task.get("due_time"), settings["time"], tz)
                except ValueError:
                    continue  # hora inexistente por cambio de horario
                if scheduled > now or now - scheduled >= RETRY_WINDOW:
                    continue
                targets = (["app"] if settings["app"] else []) \
                    + (["telegram"] if settings["telegram"] else []) \
                    + ([f"push:{s['id']}" for s in subscriptions] if settings["browser"] else [])
                for target in targets:
                    if _delivery_status(role, task["id"], scheduled, target) in ("sent", "invalid"):
                        continue
                    if not reminders.within_notification_hours(reminders.utc_now(), tz, start, end):
                        return counts
                    if target == "app":
                        _record(role, task["id"], scheduled, "app", "sent")  # entrada de la campana
                        counts["sent"] += 1
                    elif target == "telegram":
                        _send_telegram(role, task, scheduled, token_loader, chat_loader, public_url, counts)
                    else:
                        sub = next(s for s in subscriptions if f"push:{s['id']}" == target)
                        _send_push(role, task, scheduled, sub, counts)
        return counts
    finally:
        q1("SELECT RELEASE_LOCK(%s) AS ok", (LOCK_NAME,))


def _send_telegram(role, task, scheduled, token_loader, chat_loader, public_url, counts) -> None:
    try:
        reminders.send_telegram(token_loader(), chat_loader(), _message(role, task, public_url))
    except Exception as exc:  # los mensajes pueden contener credenciales o URLs
        rollback()
        _record(role, task["id"], scheduled, "telegram", "failed",
                f"Error de envío ({type(exc).__name__}); se reintentará.")
        counts["failed"] += 1
        return
    _record(role, task["id"], scheduled, "telegram", "sent")
    counts["sent"] += 1


def _send_push(role, task, scheduled, subscription, counts) -> None:
    target = f"push:{subscription['id']}"
    payload = {
        "title": f"{_emoji(role)} {role_tag(role)}",
        "body": task["title"],
        "url": f"{(subscription.get('scope_path') or '').rstrip('/')}/tasks/{task['id']}/edit",
        "tag": f"gtd-{role}-{task['id']}-{scheduled.isoformat()}",
    }
    try:
        send_browser_push(subscription, payload)
    except (WebPushException, RequestException, VapidException, OSError, ValueError) as exc:
        response = getattr(exc, "response", None)
        if isinstance(exc, WebPushException) and response is not None and response.status_code in (404, 410):
            exec_sql("UPDATE push_subscriptions SET active=0 WHERE id=%s", (subscription["id"],))
            _record(role, task["id"], scheduled, target, "invalid",
                    "Suscripción caducada; vuelve a activar el dispositivo.")
        else:
            _record(role, task["id"], scheduled, target, "failed",
                    f"Error de envío ({type(exc).__name__}); se reintentará.")
        counts["failed"] += 1
        return
    _record(role, task["id"], scheduled, target, "sent")
    counts["sent"] += 1


# --- Campana (app) ---

def bell_items(cutoff: datetime) -> List[Dict[str, Any]]:
    """Avisos con destino 'app' enviados desde ``cutoff``, con los demás canales usados."""
    rows = q(
        "SELECT a.id, a.role, a.task_id, a.scheduled_at_utc, a.updated_at_utc, a.read_at_utc, t.title "
        "FROM role_alert_deliveries a JOIN tasks t ON t.id=a.task_id "
        "WHERE a.target='app' AND a.status='sent' AND a.updated_at_utc >= %s",
        (cutoff,),
    ) or []
    if not rows:
        return []
    others = q(
        "SELECT a.role, a.task_id, a.scheduled_at_utc, a.target, s.device_name "
        "FROM role_alert_deliveries a "
        "LEFT JOIN push_subscriptions s ON a.target=CONCAT('push:', s.id) "
        "WHERE a.target<>'app' AND a.status='sent' AND a.updated_at_utc >= %s",
        (cutoff - RETRY_WINDOW,),
    ) or []
    channels: Dict[tuple, List[str]] = {}
    for o in others:
        label = "Telegram" if o["target"] == "telegram" else f"Navegador: {o['device_name'] or 'dispositivo'}"
        channels.setdefault((o["role"], o["task_id"], o["scheduled_at_utc"]), []).append(label)
    for row in rows:
        row["channels"] = ["App"] + channels.get((row["role"], row["task_id"], row["scheduled_at_utc"]), [])
        row["label"] = f"{_emoji(row['role'])} {role_tag(row['role'])}"
    return rows


def mark_bell_item_read(alert_id: int) -> bool:
    """Marca como leída una entrada de la campana. False si no existe. No hace commit."""
    if not q1("SELECT id FROM role_alert_deliveries WHERE id=%s AND target='app' AND status='sent'", (alert_id,)):
        return False
    exec_sql("UPDATE role_alert_deliveries SET read_at_utc=COALESCE(read_at_utc, %s) WHERE id=%s",
             (reminders.utc_now(), alert_id))
    return True
