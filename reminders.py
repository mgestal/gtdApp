from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
from browser_push import PUSH_SCHEMA, WebPushException, send_browser_push
from py_vapid import VapidException
from requests.exceptions import RequestException

REMINDER_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS task_reminders (
        id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        task_id INT NOT NULL,
        kind VARCHAR(16) NOT NULL,
        fixed_at_utc DATETIME DEFAULT NULL,
        offset_minutes INT DEFAULT NULL,
        date_only_time TIME DEFAULT NULL,
        created_at_utc DATETIME NOT NULL,
        KEY idx_reminders_task (task_id),
        CONSTRAINT fk_reminders_task FOREIGN KEY (task_id)
            REFERENCES tasks(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS reminder_deliveries (
        id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        reminder_id INT NOT NULL,
        scheduled_at_utc DATETIME NOT NULL,
        status VARCHAR(16) NOT NULL,
        attempts INT NOT NULL DEFAULT 0,
        updated_at_utc DATETIME NOT NULL,
        last_error VARCHAR(255) DEFAULT NULL,
        UNIQUE KEY uq_reminder_occurrence (reminder_id, scheduled_at_utc),
        KEY idx_reminder_delivery_status (status),
        CONSTRAINT fk_delivery_reminder FOREIGN KEY (reminder_id)
            REFERENCES task_reminders(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS reminder_worker_status (
        id INT NOT NULL PRIMARY KEY,
        last_run_utc DATETIME NOT NULL,
        last_error VARCHAR(255) DEFAULT NULL
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
)


def local_to_utc(value: datetime, tz: ZoneInfo) -> datetime:
    """Interpret a wall-clock time in the configured zone, rejecting DST gaps."""
    aware = value.replace(tzinfo=tz, fold=0)
    utc = aware.astimezone(timezone.utc)
    if utc.astimezone(tz).replace(tzinfo=None) != value:
        raise ValueError("La hora elegida no existe por el cambio de horario.")
    return utc.replace(tzinfo=None)


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def notification_hours(config: dict[str, Any]) -> tuple[time, time]:
    settings = config.get("notifications", {})
    if not isinstance(settings, dict):
        raise ValueError("La configuración de notificaciones no es válida.")
    try:
        start = datetime.strptime(settings.get("start_time", "08:00"), "%H:%M").time()
        end = datetime.strptime(settings.get("end_time", "20:00"), "%H:%M").time()
    except (TypeError, ValueError) as exc:
        raise ValueError("El horario de notificaciones debe usar HH:MM.") from exc
    return start, end


def within_notification_hours(now_utc: datetime, tz: ZoneInfo, start: time, end: time) -> bool:
    local = now_utc.replace(tzinfo=timezone.utc).astimezone(tz).time().replace(second=0, microsecond=0)
    if start <= end:
        return start <= local <= end
    return local >= start or local <= end


def load_reminder_token() -> str:
    from telegram_gtdAppbot import load_bot_token

    root = Path(__file__).resolve().parent
    if (root / ".token-gtdapp").is_file():
        return load_bot_token()
    token_file = root / "instance" / ".token-gtdapp"
    if not token_file.is_file():
        raise RuntimeError("No existe el fichero .token-gtdapp en la raíz ni en instance/")
    token = token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise RuntimeError("El fichero .token-gtdapp está vacío")
    return token


def db_time(value: Any) -> time | None:
    if value is None:
        return None
    if isinstance(value, timedelta):
        seconds = int(value.total_seconds())
        if not 0 <= seconds < 86400:
            raise ValueError("Hora fuera del intervalo válido.")
        return time(seconds // 3600, seconds % 3600 // 60, seconds % 60)
    if isinstance(value, time):
        return value
    raise ValueError("Hora de la tarea no válida.")


def scheduled_at(reminder: dict[str, Any], tz: ZoneInfo) -> datetime | None:
    if reminder["kind"] == "fixed":
        return reminder["fixed_at_utc"]
    if reminder["kind"] != "relative":
        raise ValueError("Tipo de recordatorio no válido.")
    due_date = reminder.get("due_date")
    if not isinstance(due_date, date):
        return None
    due_time = db_time(reminder.get("due_time"))
    if due_time is None:
        due_time = db_time(reminder.get("date_only_time"))
        if due_time is None:
            return None
    local_due = datetime.combine(due_date, due_time)
    return local_to_utc(local_due - timedelta(minutes=int(reminder["offset_minutes"])), tz)


def send_telegram(token: str, chat_id: int, text: str) -> None:
    from telegram import Bot

    async def send() -> None:
        async with Bot(token) as bot:
            await bot.send_message(chat_id=chat_id, text=text)

    asyncio.run(send())


def send_push_occurrence(conn: Any, reminder: dict[str, Any], scheduled: datetime,
                         tz: ZoneInfo, start: time, end: time, counts: dict[str, int]) -> None:
    now = utc_now()
    with conn.cursor() as cur:
        cur.execute("SELECT id, status FROM push_occurrences WHERE reminder_id=%s AND scheduled_at_utc=%s",
                    (reminder["id"], scheduled))
        occurrence = cur.fetchone()
        if not occurrence:
            cur.execute("INSERT INTO push_occurrences(reminder_id,scheduled_at_utc,status) VALUES(%s,%s,%s)",
                        (reminder["id"], scheduled, "expired" if now - scheduled >= timedelta(hours=24) else "pending"))
            occurrence = {"id": cur.lastrowid, "status": "expired" if now - scheduled >= timedelta(hours=24) else "pending"}
        if occurrence["status"] == "expired":
            conn.commit()
            return
        if now - scheduled >= timedelta(hours=24):
            cur.execute("UPDATE push_occurrences SET status='expired' WHERE id=%s", (occurrence["id"],))
            cur.execute("UPDATE push_deliveries SET status='expired', updated_at_utc=%s "
                        "WHERE occurrence_id=%s AND status IN ('pending','failed')", (now, occurrence["id"]))
            conn.commit()
            return
        if occurrence["status"] in ("pending", "no_devices"):
            cur.execute("INSERT INTO push_deliveries(occurrence_id,subscription_id,status,updated_at_utc) "
                        "SELECT %s,id,'pending',%s FROM push_subscriptions WHERE active=1", (occurrence["id"], now))
            if cur.rowcount:
                cur.execute("UPDATE push_occurrences SET status='initialized' WHERE id=%s", (occurrence["id"],))
        cur.execute("SELECT id FROM push_deliveries WHERE occurrence_id=%s AND status IN ('pending','failed')",
                    (occurrence["id"],))
        delivery_ids = [row["id"] for row in cur.fetchall()]
        if not delivery_ids:
            cur.execute("SELECT COUNT(*) AS n FROM push_deliveries WHERE occurrence_id=%s", (occurrence["id"],))
            if not cur.fetchone()["n"]:
                cur.execute("UPDATE push_occurrences SET status='no_devices' WHERE id=%s", (occurrence["id"],))
        conn.commit()

    for delivery_id in delivery_ids:
        if not within_notification_hours(utc_now(), tz, start, end):
            counts["outside_window"] = 1
            break
        with conn.cursor() as cur:
            cur.execute(
                "SELECT d.id AS delivery_id,d.subscription_id,d.status,s.*,r.task_id,r.kind,r.fixed_at_utc,r.offset_minutes,r.date_only_time,"
                "r.send_browser,t.title,t.due_date,t.due_time,t.completed_at,t.archived,t.deleted_at,"
                "t.project_id,p.archived AS project_archived,p.deleted_at AS project_deleted_at "
                "FROM push_deliveries d JOIN push_subscriptions s ON s.id=d.subscription_id "
                "JOIN push_occurrences o ON o.id=d.occurrence_id JOIN task_reminders r ON r.id=o.reminder_id "
                "JOIN tasks t ON t.id=r.task_id LEFT JOIN projects p ON p.id=t.project_id "
                "WHERE d.id=%s FOR UPDATE", (delivery_id,))
            row = cur.fetchone()
            if row and not row["active"] and row["status"] in ("pending", "failed"):
                cur.execute("UPDATE push_deliveries SET status='invalid',updated_at_utc=%s,last_error=%s WHERE id=%s",
                            (utc_now(), "Suscripción caducada; vuelve a activar el dispositivo.", delivery_id))
                conn.commit()
                continue
            if (not row or row["status"] not in ("pending", "failed") or not row["send_browser"]
                    or row["completed_at"] or row["archived"] or row["deleted_at"]
                    or (row["project_id"] is not None and
                        (row["project_archived"] is None or row["project_archived"] or row["project_deleted_at"]))
                    or scheduled_at(row, tz) != scheduled):
                conn.rollback()
                continue
            if utc_now() - scheduled >= timedelta(hours=24):
                cur.execute("UPDATE push_deliveries SET status='expired',updated_at_utc=%s WHERE id=%s",
                            (utc_now(), delivery_id))
                conn.commit()
                continue
            payload = {"title": "🔔 Recordatorio", "body": row["title"],
                       "url": f"{row['scope_path'].rstrip('/')}/tasks/{row['task_id']}/edit",
                       "tag": f"gtd-reminder-{reminder['id']}-{scheduled.isoformat()}"}
            try:
                send_browser_push(row, payload)
            except (WebPushException, RequestException, VapidException, OSError, ValueError) as exc:
                status_code = exc.response.status_code if isinstance(exc, WebPushException) and exc.response is not None else None
                if status_code in (404, 410):
                    cur.execute("UPDATE push_subscriptions SET active=0 WHERE id=%s", (row["subscription_id"],))
                    cur.execute("UPDATE push_deliveries SET status='invalid',attempts=attempts+1,"
                                "updated_at_utc=%s,last_error=%s WHERE id=%s",
                                (utc_now(), "Suscripción caducada; vuelve a activar el dispositivo.", delivery_id))
                    counts["failed"] += 1
                else:
                    cur.execute("UPDATE push_deliveries SET status='failed',attempts=attempts+1,"
                                "updated_at_utc=%s,last_error=%s WHERE id=%s",
                                (utc_now(), f"Error de envío ({type(exc).__name__}); se reintentará.", delivery_id))
                    counts["failed"] += 1
            else:
                cur.execute("UPDATE push_deliveries SET status='sent',attempts=attempts+1,"
                            "updated_at_utc=%s,last_error=NULL WHERE id=%s", (utc_now(), delivery_id))
                counts["sent"] += 1
            conn.commit()


def run_worker(conn: Any, tz: ZoneInfo, token_loader: Any, chat_loader: Any,
               public_url: str = "", start: time = time(8), end: time = time(20)) -> dict[str, int]:
    """Run one serialized scan; a unique delivery row prevents ordinary duplicates."""
    counts = {"sent": 0, "failed": 0, "expired": 0}
    with conn.cursor() as cur:
        cur.execute("SELECT GET_LOCK(%s, 0) AS acquired", ("gtdapp_reminders",))
        if not cur.fetchone()["acquired"]:
            raise RuntimeError("Ya hay un proceso de recordatorios en ejecución.")
    try:
        now = utc_now()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO reminder_worker_status(id, last_run_utc, last_error) VALUES(1,%s,NULL) "
                "ON DUPLICATE KEY UPDATE last_run_utc=VALUES(last_run_utc), last_error=NULL",
                (now,),
            )
            conn.commit()
            if not within_notification_hours(now, tz, start, end):
                counts["outside_window"] = 1
                return counts
            cur.execute(
                "SELECT r.id FROM task_reminders r JOIN tasks t ON t.id=r.task_id "
                "LEFT JOIN projects p ON p.id=t.project_id "
                "WHERE t.completed_at IS NULL AND t.archived=0 AND t.deleted_at IS NULL "
                "AND (t.project_id IS NULL OR (p.archived=0 AND p.deleted_at IS NULL)) ORDER BY r.id"
            )
            ids = [row["id"] for row in cur.fetchall()]
        conn.commit()

        for reminder_id in ids:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT r.*, t.title, t.due_date, t.due_time, t.completed_at, t.archived, "
                    "t.deleted_at, t.project_id, p.archived AS project_archived, "
                    "p.deleted_at AS project_deleted_at FROM task_reminders r "
                    "JOIN tasks t ON t.id=r.task_id LEFT JOIN projects p ON p.id=t.project_id "
                    "WHERE r.id=%s FOR UPDATE",
                    (reminder_id,),
                )
                reminder = cur.fetchone()
                if (not reminder or reminder["completed_at"] or reminder["archived"]
                        or reminder["deleted_at"] or (reminder["project_id"] is not None
                        and (reminder["project_archived"] is None or reminder["project_archived"]
                             or reminder["project_deleted_at"]))):
                    conn.rollback()
                    continue
                try:
                    scheduled = scheduled_at(reminder, tz)
                except ValueError as exc:
                    cur.execute("UPDATE reminder_worker_status SET last_error=%s WHERE id=1",
                                (f"Recordatorio {reminder_id}: {exc}"[:255],))
                    conn.commit()
                    counts["failed"] += 1
                    continue
                now = utc_now()
                if not within_notification_hours(now, tz, start, end):
                    conn.rollback()
                    counts["outside_window"] = 1
                    break
                if scheduled is None or scheduled > now:
                    conn.rollback()
                    continue
                if reminder["send_browser"]:
                    send_push_occurrence(conn, reminder, scheduled, tz, start, end, counts)
                    if counts.get("outside_window"):
                        break
                    cur.execute(
                        "SELECT r.*,t.due_date,t.due_time,t.completed_at,t.archived,t.deleted_at,"
                        "t.project_id,p.archived AS project_archived,p.deleted_at AS project_deleted_at "
                        "FROM task_reminders r JOIN tasks t ON t.id=r.task_id "
                        "LEFT JOIN projects p ON p.id=t.project_id WHERE r.id=%s FOR UPDATE", (reminder_id,))
                    current = cur.fetchone()
                    if (not current or not current["send_telegram"] or current["completed_at"]
                            or current["archived"] or current["deleted_at"]
                            or (current["project_id"] is not None and
                                (current["project_archived"] is None or current["project_archived"] or current["project_deleted_at"]))
                            or scheduled_at(current, tz) != scheduled):
                        conn.rollback()
                        continue
                elif not reminder["send_telegram"]:
                    conn.rollback()
                    continue
                cur.execute(
                    "SELECT id, status FROM reminder_deliveries WHERE reminder_id=%s AND scheduled_at_utc=%s",
                    (reminder_id, scheduled),
                )
                delivery = cur.fetchone()
                if delivery and delivery["status"] in ("sent", "expired"):
                    conn.rollback()
                    continue
                if not delivery:
                    cur.execute(
                        "INSERT INTO reminder_deliveries(reminder_id, scheduled_at_utc, status, updated_at_utc) "
                        "VALUES(%s,%s,'pending',%s)",
                        (reminder_id, scheduled, now),
                    )
                    delivery_id = cur.lastrowid
                else:
                    delivery_id = delivery["id"]
                if now - scheduled >= timedelta(hours=24):
                    cur.execute(
                        "UPDATE reminder_deliveries SET status='expired', updated_at_utc=%s WHERE id=%s",
                        (now, delivery_id),
                    )
                    conn.commit()
                    counts["expired"] += 1
                    continue
                text = f"🔔 Recordatorio\n\n{reminder['title']}"
                if reminder["due_date"]:
                    text += f"\nVence: {reminder['due_date'].strftime('%d/%m/%Y')}"
                    due_time = db_time(reminder["due_time"])
                    if due_time:
                        text += f" a las {due_time.strftime('%H:%M')}"
                if public_url:
                    text += f"\n\n{public_url.rstrip('/')}/tasks/{reminder['task_id']}/edit"
                if not within_notification_hours(utc_now(), tz, start, end):
                    conn.rollback()
                    counts["outside_window"] = 1
                    break
                try:
                    send_telegram(token_loader(), chat_loader(), text)
                except Exception as exc:
                    # Exception messages can contain credentials or request URLs.
                    error = f"Error de envío ({type(exc).__name__}); se reintentará."
                    cur.execute(
                        "UPDATE reminder_deliveries SET status='failed', attempts=attempts+1, "
                        "updated_at_utc=%s, last_error=%s WHERE id=%s",
                        (now, error, delivery_id),
                    )
                    counts["failed"] += 1
                else:
                    cur.execute(
                        "UPDATE reminder_deliveries SET status='sent', attempts=attempts+1, "
                        "updated_at_utc=%s, last_error=NULL WHERE id=%s",
                        (now, delivery_id),
                    )
                    counts["sent"] += 1
                conn.commit()
        return counts
    except Exception as exc:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO reminder_worker_status(id, last_run_utc, last_error) VALUES(1,%s,%s) "
                "ON DUPLICATE KEY UPDATE last_run_utc=VALUES(last_run_utc), last_error=VALUES(last_error)",
                (utc_now(), f"Error del proceso ({type(exc).__name__})"),
            )
        conn.commit()
        raise
    finally:
        with conn.cursor() as cur:
            cur.execute("SELECT RELEASE_LOCK(%s)", ("gtdapp_reminders",))
