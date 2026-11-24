"""Recordatorios de tareas, centro de notificaciones, avisos por ubicación y Web Push."""
from __future__ import annotations

import hmac
import io
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import pymysql
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from flask import (
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from py_vapid import VapidException
from requests.exceptions import RequestException

from browser_push import (
    DEFAULT_PUSH_DURATION,
    endpoint_hash,
    KEY_PATH,
    public_key,
    PUSH_DURATIONS,
    push_expiry,
    push_usable_sql,
    send_browser_push,
    validate_subscription,
    WebPushException,
)
from reminders import (
    db_time,
    load_reminder_token,
    local_to_utc,
    notification_hours,
    scheduled_at,
    utc_now,
    within_notification_hours,
)

from gtd.config import load_config
from gtd.utils import safe_next_url
from gtd.core import app
from gtd.db import commit, exec_sql, q, q1, rollback
from gtd.common import admin_required


@app.route("/api/location-alerts")
def location_alerts():
    rows = q(
            "SELECT tg.id, tg.name, loc.latitude, loc.longitude, loc.radius_m, t.id AS task_id "
            "FROM tag_locations loc JOIN tags tg ON tg.id=loc.tag_id "
            "JOIN task_tags tt ON tt.tag_id=tg.id JOIN tasks t ON t.id=tt.task_id "
            "LEFT JOIN projects p ON p.id=t.project_id "
            "WHERE t.completed_at IS NULL AND t.archived=0 AND t.deleted_at IS NULL "
            "AND (t.project_id IS NULL OR (p.archived=0 AND p.deleted_at IS NULL)) "
            "ORDER BY tg.name, t.id"
    )
    locations = {}
    for row in rows:
        location = locations.setdefault(row["id"], {
            "id": row["id"], "name": row["name"], "latitude": row["latitude"],
            "longitude": row["longitude"], "radius_m": row["radius_m"], "task_ids": [],
            "url": url_for("tag_detail", tag_id=row["id"], location_pending=1),
        })
        location["task_ids"].append(row["task_id"])
    response = jsonify({"within_hours": push_test_within_hours(), "locations": list(locations.values())})
    response.headers["Cache-Control"] = "no-store"
    return response


def notification_history_days(cfg):
    value = cfg.get("app", {}).get("notifications", {}).get("history_days", 1)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 365:
        raise ValueError("Los días de historial de notificaciones deben estar entre 1 y 365.")
    return value


@app.route("/api/notifications")
def notification_history():
    cfg = load_config()
    cutoff = utc_now() - timedelta(days=notification_history_days(cfg))
    rows = q(
        "SELECT r.id AS reminder_id, r.task_id, t.title, d.scheduled_at_utc, d.updated_at_utc, "
        "'Telegram' AS channel, NULL AS device, nr.read_at_utc "
        "FROM reminder_deliveries d JOIN task_reminders r ON r.id=d.reminder_id "
        "JOIN tasks t ON t.id=r.task_id "
        "LEFT JOIN notification_reads nr ON nr.reminder_id=r.id AND nr.scheduled_at_utc=d.scheduled_at_utc "
        "WHERE d.status='sent' AND d.updated_at_utc >= %s "
        "UNION ALL "
        "SELECT r.id, r.task_id, t.title, o.scheduled_at_utc, d.updated_at_utc, "
        "'Navegador', s.device_name, nr.read_at_utc "
        "FROM push_deliveries d JOIN push_occurrences o ON o.id=d.occurrence_id "
        "JOIN push_subscriptions s ON s.id=d.subscription_id "
        "JOIN task_reminders r ON r.id=o.reminder_id JOIN tasks t ON t.id=r.task_id "
        "LEFT JOIN notification_reads nr ON nr.reminder_id=r.id AND nr.scheduled_at_utc=o.scheduled_at_utc "
        "WHERE d.status='sent' AND d.updated_at_utc >= %s",
        (cutoff, cutoff),
    )
    zone = ZoneInfo(cfg.get("app", {}).get("timezone", "Europe/Madrid"))
    grouped = {}
    for row in rows:
        key = (row["reminder_id"], row["scheduled_at_utc"])
        item = grouped.setdefault(key, {
            "reminder_id": row["reminder_id"], "scheduled_at": row["scheduled_at_utc"].isoformat(),
            "title": row["title"], "url": url_for("task_edit", task_id=row["task_id"]),
            "read": row["read_at_utc"] is not None, "channels": [], "updated_at": row["updated_at_utc"],
        })
        label = f"Navegador: {row['device']}" if row["device"] is not None else row["channel"]
        if label not in item["channels"]:
            item["channels"].append(label)
        item["updated_at"] = max(item["updated_at"], row["updated_at_utc"])
    result = sorted(grouped.values(), key=lambda item: item["updated_at"], reverse=True)
    for item in result:
        item["updated_at"] = item["updated_at"].replace(tzinfo=ZoneInfo("UTC")).astimezone(zone).strftime("%d-%m-%Y %H:%M")
    response = jsonify({"notifications": result})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/notifications/read", methods=["POST"])
def notification_mark_read():
    supplied = request.headers.get("X-GTD-Notification-CSRF", "")
    expected = session.get("notification_csrf", "")
    if not expected or not hmac.compare_digest(supplied, expected):
        abort(403)
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or type(data.get("reminder_id")) is not int or data["reminder_id"] <= 0:
        return jsonify({"error": "Recordatorio no válido."}), 400
    try:
        scheduled = datetime.fromisoformat(data.get("scheduled_at", ""))
        if scheduled.tzinfo is not None or scheduled.microsecond:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "Fecha de recordatorio no válida."}), 400
    reminder_id = data["reminder_id"]
    sent = q1(
        "SELECT 1 AS sent FROM reminder_deliveries WHERE reminder_id=%s AND scheduled_at_utc=%s AND status='sent' "
        "UNION ALL SELECT 1 FROM push_occurrences o JOIN push_deliveries d ON d.occurrence_id=o.id "
        "WHERE o.reminder_id=%s AND o.scheduled_at_utc=%s AND d.status='sent' LIMIT 1",
        (reminder_id, scheduled, reminder_id, scheduled),
    )
    if not sent:
        return jsonify({"error": "No se encontró el recordatorio enviado."}), 404
    exec_sql("INSERT IGNORE INTO notification_reads(reminder_id, scheduled_at_utc, read_at_utc) VALUES(%s,%s,%s)",
             (reminder_id, scheduled, utc_now()))
    commit()
    return jsonify({"ok": True})


def parse_reminder_form(form, task, tz):
    kind = form.get("kind")
    if kind == "fixed":
        local = datetime.strptime(form.get("fixed_at", ""), "%Y-%m-%dT%H:%M")
        fixed = local_to_utc(local, tz)
        return kind, fixed, None, None, fixed
    if kind != "relative":
        raise ValueError("Selecciona un tipo de recordatorio.")
    if not task["due_date"]:
        raise ValueError("Guarda primero una fecha de finalización para la tarea.")
    amount = int(form.get("amount", ""))
    units = {"minutes": 1, "hours": 60, "days": 1440}
    unit = form.get("unit")
    if amount < 0 or unit not in units or amount * units[unit] > 5256000:
        raise ValueError("El intervalo debe estar entre cero y diez años.")
    offset = amount * units[unit]
    fallback_raw = (form.get("date_only_time") or "").strip()
    fallback_time = datetime.strptime(fallback_raw, "%H:%M").time() if fallback_raw else None
    if not task["due_time"] and fallback_time is None:
        raise ValueError("Indica la hora de referencia para una tarea sin hora.")
    when = scheduled_at({"kind": kind, "offset_minutes": offset,
                         "date_only_time": fallback_time, **task}, tz)
    return kind, None, offset, fallback_time, when


@app.route("/tasks/<int:task_id>/reminders/preview", methods=["POST"])
def task_reminder_preview(task_id: int):
    task = q1("SELECT id, due_date, due_time FROM tasks WHERE id=%s", (task_id,))
    if not task:
        abort(404)
    tz = ZoneInfo(load_config().get("app", {}).get("timezone", "Europe/Madrid"))
    try:
        *_, when = parse_reminder_form(request.form, task, tz)
        if when is None or when <= utc_now():
            raise ValueError("El recordatorio debe estar programado para una fecha futura.")
        local = when.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
        return jsonify({"ok": True, "message": (
            f"Programado para el {local:%d-%m-%Y a las %H:%M} ({tz.key}); "
            "se intentará enviar en la siguiente revisión dentro del horario permitido."
        )})
    except ValueError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400


@app.route("/tasks/<int:task_id>/reminders", methods=["POST"])
@app.route("/tasks/<int:task_id>/reminders/<int:reminder_id>/edit", methods=["POST"])
def task_add_reminder(task_id: int, reminder_id: Optional[int] = None):
    task = q1("SELECT id, due_date, due_time FROM tasks WHERE id=%s", (task_id,))
    if not task:
        abort(404)
    existing = None
    if reminder_id is not None:
        existing = q1("SELECT * FROM task_reminders WHERE id=%s AND task_id=%s", (reminder_id, task_id))
        if not existing:
            abort(404)
    next_url = safe_next_url(request.form.get("next"))
    target = url_for("task_edit", task_id=task_id, next=next_url) + "#recordatorios"
    tz = ZoneInfo(load_config().get("app", {}).get("timezone", "Europe/Madrid"))
    try:
        kind, fixed, offset, fallback_time, when = parse_reminder_form(request.form, task, tz)
        send_telegram_flag = request.form.get("send_telegram") == "1"
        send_browser_flag = request.form.get("send_browser") == "1"
        if not send_telegram_flag and not send_browser_flag:
            raise ValueError("Selecciona al menos un canal de notificación.")
        changed = not existing or (
            existing["kind"], existing["fixed_at_utc"], existing["offset_minutes"],
            db_time(existing["date_only_time"])
        ) != (kind, fixed, offset, fallback_time)
        if changed and (when is None or when <= utc_now()):
            raise ValueError("El recordatorio debe estar programado para una fecha futura.")
        if existing and changed:
            exec_sql("UPDATE task_reminders SET kind=%s, fixed_at_utc=%s, offset_minutes=%s, "
                     "date_only_time=%s, send_telegram=%s, send_browser=%s WHERE id=%s AND task_id=%s",
                     (kind, fixed, offset, fallback_time, send_telegram_flag, send_browser_flag, reminder_id, task_id))
            exec_sql("DELETE FROM reminder_deliveries WHERE reminder_id=%s", (reminder_id,))
            exec_sql("DELETE FROM push_occurrences WHERE reminder_id=%s", (reminder_id,))
        elif existing:
            exec_sql("UPDATE task_reminders SET send_telegram=%s,send_browser=%s WHERE id=%s AND task_id=%s",
                     (send_telegram_flag, send_browser_flag, reminder_id, task_id))
        elif not existing:
            exec_sql(
                "INSERT INTO task_reminders(task_id, kind, fixed_at_utc, offset_minutes, date_only_time, created_at_utc, send_telegram, send_browser) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (task_id, kind, fixed, offset, fallback_time, utc_now(), send_telegram_flag, send_browser_flag),
            )
        commit()
        flash("Recordatorio actualizado." if existing else "Recordatorio añadido.", "ok")
    except ValueError as exc:
        flash(f"Recordatorio no válido: {exc}", "error")
    except Exception:
        rollback()
        app.logger.exception("No se pudo guardar el recordatorio de la tarea %s", task_id)
        flash("No se pudo guardar el recordatorio.", "error")
    return redirect(target)


@app.route("/tasks/<int:task_id>/reminders/<int:reminder_id>/delete", methods=["POST"])
def task_delete_reminder(task_id: int, reminder_id: int):
    next_url = safe_next_url(request.form.get("next"))
    target = url_for("task_edit", task_id=task_id, next=next_url) + "#recordatorios"
    if not q1("SELECT id FROM task_reminders WHERE id=%s AND task_id=%s", (reminder_id, task_id)):
        abort(404)
    try:
        exec_sql("DELETE FROM task_reminders WHERE id=%s AND task_id=%s", (reminder_id, task_id))
        commit()
        flash("Recordatorio eliminado.", "ok")
    except Exception:
        rollback()
        app.logger.exception("No se pudo eliminar el recordatorio %s", reminder_id)
        flash("No se pudo eliminar el recordatorio.", "error")
    return redirect(target)


def require_push_admin():
    if not admin_required():
        abort(403)
    supplied = request.headers.get("X-GTD-Push-CSRF", "")
    expected = session.get("push_csrf", "")
    if not expected or not hmac.compare_digest(supplied, expected):
        abort(403)


@app.route("/admin/push/key", methods=["POST"])
def admin_push_key():
    require_push_admin()
    try:
        if not KEY_PATH.exists() and q1("SELECT id FROM push_subscriptions LIMIT 1"):
            return jsonify({"ok": False, "error": "Falta la clave VAPID existente. Restáurala o elimina todas las suscripciones antes de crear otra."}), 409
        return jsonify({"ok": True, "publicKey": public_key(create=True)})
    except (OSError, ValueError) as exc:
        app.logger.exception("No se pudo preparar la clave de Web Push")
        return jsonify({"ok": False, "error": f"No se pudo preparar Web Push ({type(exc).__name__})."}), 500


@app.route("/admin/push/subscribe", methods=["POST"])
def admin_push_subscribe():
    require_push_admin()
    data = request.get_json(silent=True)
    try:
        if not isinstance(data, dict):
            raise ValueError("Datos de suscripción no válidos.")
        endpoint, p256dh, auth_key = validate_subscription(data.get("subscription"))
        origin = data.get("origin", "")
        parsed = urlparse(origin)
        if (len(origin) > 255 or parsed.scheme != "https" or parsed.netloc != request.host
                or parsed.path or parsed.query or parsed.fragment or parsed.username):
            raise ValueError("Origen de suscripción no válido; se requiere HTTPS.")
        name = data.get("name", "")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
            raise ValueError("Indica un nombre de dispositivo de hasta 100 caracteres.")
        scope_path = request.script_root or ""
        if len(scope_path) > 255:
            raise ValueError("La ruta base es demasiado larga.")
        public_key()
        now = utc_now()
        expires_at = push_expiry(data.get("duration", DEFAULT_PUSH_DURATION), now)
        exec_sql(
            "INSERT INTO push_subscriptions(endpoint_hash,endpoint,p256dh,auth_key,device_name,scope_path,origin,"
            "expires_at_utc,created_at_utc,updated_at_utc) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
            "p256dh=VALUES(p256dh),auth_key=VALUES(auth_key),device_name=VALUES(device_name),"
            "scope_path=VALUES(scope_path),origin=VALUES(origin),active=1,"
            "expires_at_utc=VALUES(expires_at_utc),updated_at_utc=VALUES(updated_at_utc)",
            (endpoint_hash(endpoint), endpoint, p256dh, auth_key, name.strip(), scope_path, origin,
             expires_at, now, now),
        )
        commit()
        return jsonify({"ok": True, "expires_at_utc": expires_at.isoformat() if expires_at else None})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except (pymysql.MySQLError, OSError) as exc:
        rollback()
        app.logger.exception("No se pudo registrar la suscripción Web Push")
        return jsonify({"ok": False, "error": f"No se pudo registrar el dispositivo ({type(exc).__name__})."}), 500


@app.route("/admin/push/unsubscribe", methods=["POST"])
def admin_push_unsubscribe():
    require_push_admin()
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get("endpoint"), str):
        return jsonify({"ok": False, "error": "Suscripción no válida."}), 400
    exec_sql("DELETE FROM push_subscriptions WHERE endpoint_hash=%s", (endpoint_hash(data["endpoint"]),))
    commit()
    return jsonify({"ok": True})


@app.route("/admin/push/status", methods=["POST"])
def admin_push_status():
    require_push_admin()
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get("endpoint"), str):
        return jsonify({"ok": False, "error": "Suscripción no válida."}), 400
    row = q1("SELECT id, device_name, expires_at_utc, " + push_usable_sql() + " AS usable "
             "FROM push_subscriptions WHERE endpoint_hash=%s AND active=1", (endpoint_hash(data["endpoint"]),))
    if row and not row["usable"]:
        return jsonify({"ok": True, "registered": False, "expired": True, "name": row["device_name"],
                        "expires_at": push_expiry_local(row["expires_at_utc"])})
    return jsonify({"ok": True, "registered": bool(row), "name": row["device_name"] if row else None,
                    "expires_at": push_expiry_local(row["expires_at_utc"]) if row else None})


def push_expiry_local(expires_at_utc: Optional[datetime]) -> Optional[str]:
    """Caducidad en hora local (dd-mm-aaaa HH:MM) o None si no caduca."""
    if not expires_at_utc:
        return None
    tz = ZoneInfo(load_config().get("app", {}).get("timezone", "Europe/Madrid"))
    return expires_at_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).strftime("%d-%m-%Y %H:%M")


@app.route("/admin/push/<int:subscription_id>/delete", methods=["POST"])
def admin_push_delete(subscription_id: int):
    require_push_admin()
    exec_sql("DELETE FROM push_subscriptions WHERE id=%s", (subscription_id,))
    commit()
    return jsonify({"ok": True})


@app.route("/admin/push/test", methods=["POST"])
def admin_push_test():
    require_push_admin()
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get("endpoint"), str):
        return jsonify({"ok": False, "error": "Suscripción no válida."}), 400
    if not push_test_within_hours():
        return jsonify({"ok": False, "error": "La prueba no se envía fuera del horario de notificaciones."}), 400
    subscription = q1("SELECT * FROM push_subscriptions WHERE endpoint_hash=%s AND " + push_usable_sql(),
                      (endpoint_hash(data["endpoint"]),))
    if not subscription:
        return jsonify({"ok": False, "error": "Este dispositivo no está suscrito o su activación ha expirado."}), 404
    error, code = send_push_test(subscription)
    if error:
        return jsonify({"ok": False, "error": error}), code
    return jsonify({"ok": True})


def push_test_within_hours():
    cfg = load_config().get("app", {})
    start, end = notification_hours(cfg)
    return within_notification_hours(utc_now(), ZoneInfo(cfg.get("timezone", "Europe/Madrid")), start, end)


def send_push_test(subscription):
    try:
        send_browser_push(subscription, {"title": "GTDApp", "body": "Prueba de notificaciones",
                                         "url": f"{subscription['scope_path'].rstrip('/')}/admin/notifications",
                                         "tag": "gtd-push-test", "renotify": True})
        return None, 200
    except WebPushException as exc:
        if exc.response is not None and exc.response.status_code in (404, 410):
            exec_sql("UPDATE push_subscriptions SET active=0 WHERE id=%s", (subscription["id"],))
            commit()
            return "La suscripción ha caducado. Vuelve a activar este dispositivo.", 410
        app.logger.warning("La prueba de Web Push falló: %s", type(exc).__name__)
        return "No se pudo enviar la prueba de Web Push.", 502
    except (OSError, ValueError, VapidException, RequestException) as exc:
        app.logger.warning("La prueba de Web Push falló: %s", type(exc).__name__)
        return f"No se pudo enviar la prueba ({type(exc).__name__}).", 502


@app.route("/admin/push/test-all", methods=["POST"])
def admin_push_test_all():
    require_push_admin()
    if not push_test_within_hours():
        return jsonify({"ok": False, "error": "La prueba no se envía fuera del horario de notificaciones."}), 400
    subscriptions = q("SELECT * FROM push_subscriptions WHERE " + push_usable_sql() + " ORDER BY id")
    if not subscriptions:
        return jsonify({"ok": False, "error": "No hay dispositivos activos suscritos."}), 400
    results = []
    for subscription in subscriptions:
        if not push_test_within_hours():
            error = "No enviada: horario de notificaciones finalizado."
        else:
            error, _ = send_push_test(subscription)
        results.append({"name": subscription["device_name"], "id": subscription["id"], "error": error})
    return jsonify({"ok": True, "results": results})


def load_notification_status(cfg):
    session.setdefault("push_csrf", secrets.token_urlsafe(32))
    from telegram_gtdAppbot import load_chat_id
    try:
        load_reminder_token()
        if not int(load_chat_id()):
            raise ValueError("Chat ID no válido")
        telegram_configured = True
        telegram_status = "Configurado; usa la prueba para comprobar la conexión."
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        telegram_configured = False
        telegram_status = f"No configurado o configuración inválida ({type(exc).__name__})."
    worker_status = q1("SELECT last_run_utc, last_error FROM reminder_worker_status WHERE id=1")
    timezone_name = cfg.get("app", {}).get("timezone", "Europe/Madrid")
    if worker_status:
        worker_status["last_run_local"] = worker_status["last_run_utc"].replace(
            tzinfo=ZoneInfo("UTC")
        ).astimezone(ZoneInfo(timezone_name)).strftime("%d-%m-%Y %H:%M:%S")
        worker_status["stale"] = utc_now() - worker_status["last_run_utc"] > timedelta(minutes=20)
    start_time, end_time = notification_hours(cfg.get("app", {}))
    failed_deliveries = q(
        "SELECT d.last_error, d.updated_at_utc, d.scheduled_at_utc, "
        "r.*, t.title, t.due_date, t.due_time FROM reminder_deliveries d "
        "JOIN task_reminders r ON r.id=d.reminder_id JOIN tasks t ON t.id=r.task_id "
        "LEFT JOIN projects p ON p.id=t.project_id WHERE d.status='failed' AND r.send_telegram=1 "
        "AND t.completed_at IS NULL AND t.archived=0 AND t.deleted_at IS NULL "
        "AND (t.project_id IS NULL OR (p.archived=0 AND p.deleted_at IS NULL)) "
        "ORDER BY d.updated_at_utc DESC"
    )
    current_failures = []
    for failure in failed_deliveries:
        try:
            current = scheduled_at(failure, ZoneInfo(timezone_name))
        except ValueError:
            continue
        if (current == failure["scheduled_at_utc"]
                and utc_now() - current < timedelta(hours=24)):
            current_failures.append(failure)
    push_failed_rows = q(
        "SELECT d.last_error, d.status AS delivery_status, o.scheduled_at_utc, "
        "s.device_name, r.*, t.title, t.due_date, t.due_time "
        "FROM push_deliveries d JOIN push_subscriptions s ON s.id=d.subscription_id "
        "JOIN push_occurrences o ON o.id=d.occurrence_id "
        "JOIN task_reminders r ON r.id=o.reminder_id JOIN tasks t ON t.id=r.task_id "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE d.status IN ('failed','invalid') AND r.send_browser=1 "
        "AND t.completed_at IS NULL AND t.archived=0 AND t.deleted_at IS NULL "
        "AND (t.project_id IS NULL OR (p.archived=0 AND p.deleted_at IS NULL)) "
        "ORDER BY d.updated_at_utc DESC"
    )
    push_errors = []
    for failure in push_failed_rows:
        try:
            current = scheduled_at(failure, ZoneInfo(timezone_name))
        except ValueError:
            continue
        if (current == failure["scheduled_at_utc"]
                and utc_now() - current < timedelta(hours=24)):
            push_errors.append(failure)
    notification_status = {
        "telegram_configured": telegram_configured,
        "telegram_status": telegram_status,
        "worker": worker_status,
        "failed": current_failures[:10],
        "start_time": start_time.strftime("%H:%M"),
        "end_time": end_time.strftime("%H:%M"),
        "within_hours": within_notification_hours(utc_now(), ZoneInfo(timezone_name), start_time, end_time),
        "timezone": timezone_name,
        "devices": [
            dict(device, expires_local=push_expiry_local(device["expires_at_utc"]),
                 expired=bool(device["expires_at_utc"] and device["expires_at_utc"] <= utc_now()))
            for device in q("SELECT id, device_name, active, expires_at_utc, created_at_utc, updated_at_utc "
                            "FROM push_subscriptions ORDER BY id")
        ],
        "push_durations": [(key, label) for key, (label, _delta) in PUSH_DURATIONS.items()],
        "push_default_duration": DEFAULT_PUSH_DURATION,
        "push_csrf": session["push_csrf"],
        "push_errors": push_errors[:10],
    }
    return notification_status


@app.route("/admin/notifications")
def admin_notifications():
    if not admin_required():
        flash("No autorizado. Inicia sesión como admin.", "error")
        return redirect(url_for("admin"))
    cfg = load_config()
    ca_status = {"configured": bool(cfg.get("app", {}).get("notifications", {}).get("ca_certificate_path"))}
    if ca_status["configured"]:
        try:
            certificate = load_notification_ca_certificate(cfg)
            ca_status["fingerprint"] = certificate.fingerprint(hashes.SHA256()).hex(":").upper()
        except (OSError, ValueError, x509.ExtensionNotFound) as exc:
            app.logger.warning("No se pudo cargar el certificado de CA: %s", type(exc).__name__)
            ca_status["error"] = "No se puede leer un certificado de CA válido; revisa la configuración y los permisos."
    return render_template("admin_notifications.html", cfg=cfg, notification_status=load_notification_status(cfg),
                           ca_status=ca_status)


def load_notification_ca_certificate(cfg: Dict[str, Any]) -> x509.Certificate:
    path = cfg.get("app", {}).get("notifications", {}).get("ca_certificate_path", "")
    if not isinstance(path, str) or not path or not Path(path).is_absolute():
        raise ValueError("La ruta del certificado de CA debe ser absoluta.")
    with Path(path).open("rb") as file:
        data = file.read(65537)
    if len(data) > 65536:
        raise ValueError("El certificado de CA es demasiado grande.")
    certificate = x509.load_pem_x509_certificate(data)
    if not certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
        raise ValueError("El certificado no pertenece a una CA.")
    return certificate


@app.route("/admin/notifications/ca-certificate")
def admin_notification_ca_certificate():
    if not admin_required():
        abort(403)
    cfg = load_config()
    if not cfg.get("app", {}).get("notifications", {}).get("ca_certificate_path"):
        abort(404)
    try:
        certificate = load_notification_ca_certificate(cfg)
    except (OSError, ValueError, x509.ExtensionNotFound) as exc:
        app.logger.warning("No se pudo descargar el certificado de CA: %s", type(exc).__name__)
        abort(503, description="No se puede leer el certificado de CA configurado.")
    response = send_file(io.BytesIO(certificate.public_bytes(serialization.Encoding.PEM)),
                         mimetype="application/x-x509-ca-cert", as_attachment=True,
                         download_name="gtdapp-ca.crt")
    response.headers["Cache-Control"] = "no-store"
    return response
