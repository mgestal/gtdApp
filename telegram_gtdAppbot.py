from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pymysql
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters


# =========================================================
# Config
# =========================================================
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
    },
}


# =========================================================
# Helpers de configuración
# =========================================================
def load_config() -> Dict[str, Any]:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return DEFAULT_CONFIG


def app_timezone() -> ZoneInfo:
    cfg = load_config()
    tz_name = cfg.get("app", {}).get("timezone", "Europe/Madrid")
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("Europe/Madrid")


def today_local() -> date:
    return datetime.now(app_timezone()).date()


def load_bot_token() -> str:
    """
    Lee el token del bot desde .token-gtdapp
    """
    token_path = BASE_DIR / ".token-gtdapp"

    if not token_path.exists():
        raise RuntimeError("No existe el fichero .token-gtdapp con el token del bot.")

    token = token_path.read_text(encoding="utf-8").strip()

    if not token:
        raise RuntimeError("El fichero .token-gtdapp está vacío.")

    return token


# =========================================================
# DB
# =========================================================
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


def q(sql: str, params: Tuple[Any, ...] = ()) -> List[Dict[str, Any]]:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())
    finally:
        conn.close()


def q1(sql: str, params: Tuple[Any, ...] = ()) -> Optional[Dict[str, Any]]:
    rows = q(sql, params)
    return rows[0] if rows else None


def exec_sql(sql: str, params: Tuple[Any, ...] = ()) -> int:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            lastrowid = cur.lastrowid or 0
        conn.commit()
        return lastrowid
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def exec_many(sql_statements: List[Tuple[str, Tuple[Any, ...]]]) -> None:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            for sql, params in sql_statements:
                cur.execute(sql, params)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# =========================================================
# Parsing de tareas desde Telegram
# Soporta:
# - texto libre
# - @etiqueta
# - *hoy
# - *mañana
# - dd/mm/aaaa
# =========================================================
TAG_RE = re.compile(r"@([A-Za-z0-9_\-áéíóúÁÉÍÓÚñÑ]+)")
STAR_DATE_RE = re.compile(r"\*(hoy|mañana)\b", re.IGNORECASE)
SLASH_DATE_RE = re.compile(r"(?<!\d)(\d{1,2}/\d{1,2}/\d{4})(?!\d)")


def normalize_name(s: str) -> str:
    return (s or "").strip()


def parse_due_token(token: str) -> date:
    token = (token or "").strip().lower()
    today = today_local()

    if token == "hoy":
        return today

    if token == "mañana":
        return today + timedelta(days=1)

    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", token)
    if m:
        day = int(m.group(1))
        month = int(m.group(2))
        year = int(m.group(3))
        return datetime.strptime(f"{day:02d}/{month:02d}/{year}", "%d/%m/%Y").date()

    raise ValueError(f"Fecha inválida: {token}")


def extract_due_date(text: str) -> tuple[Optional[date], str]:
    s = (text or "").strip()
    if not s:
        return None, s

    m = STAR_DATE_RE.search(s)
    if m:
        token = m.group(1)
        due = parse_due_token(token)
        cleaned = (s[:m.start()] + " " + s[m.end():]).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        return due, cleaned

    m = SLASH_DATE_RE.search(s)
    if m:
        token = m.group(1)
        due = parse_due_token(token)
        cleaned = (s[:m.start()] + " " + s[m.end():]).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        return due, cleaned

    return None, s


def parse_task_text(raw: str) -> tuple[str, List[str], Optional[date]]:
    due_date, cleaned = extract_due_date(raw)
    tags = [normalize_name(t) for t in TAG_RE.findall(cleaned) if normalize_name(t)]

    title = TAG_RE.sub("", cleaned)
    title = re.sub(r"\s+", " ", title).strip(" -_,.;:")

    return title, tags, due_date


# =========================================================
# Helpers de tags / tareas
# =========================================================
def get_or_create_tag(tag_name: str) -> int:
    tag_name = normalize_name(tag_name)
    row = q1("SELECT id FROM tags WHERE name=%s", (tag_name,))
    if row:
        return int(row["id"])
    return exec_sql("INSERT INTO tags(name) VALUES(%s)", (tag_name,))


def create_inbox_task_from_text(raw_text: str) -> tuple[int, str, Optional[date], List[str]]:
    title, tags, due_date = parse_task_text(raw_text)

    if not title:
        raise ValueError("No se detectó un título válido.")

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tasks(title, project_id, folder_id, due_date, recurrence_rule) "
                "VALUES(%s, NULL, NULL, %s, NULL)",
                (title, due_date),
            )
            task_id = cur.lastrowid or 0

            for tag_name in tags:
                cur.execute("SELECT id FROM tags WHERE name=%s", (tag_name,))
                row = cur.fetchone()
                if row:
                    tag_id = int(row["id"])
                else:
                    cur.execute("INSERT INTO tags(name) VALUES(%s)", (tag_name,))
                    tag_id = cur.lastrowid or 0

                cur.execute(
                    "INSERT IGNORE INTO task_tags(task_id, tag_id) VALUES(%s,%s)",
                    (task_id, tag_id),
                )

        conn.commit()
        return task_id, title, due_date, tags
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def format_task_line(row: Dict[str, Any]) -> str:
    title = row.get("title", "").strip() or "(sin título)"
    due_date = row.get("due_date")
    if due_date:
        return f"• {title} [{due_date.strftime('%d/%m/%Y')}]"
    return f"• {title}"


# =========================================================
# Comandos Telegram
# =========================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        "GtdAppBot listo.\n\n"
        "Envía un mensaje y lo crearé como tarea en Inbox.\n\n"
        "Formato soportado:\n"
        "• tarea @etiqueta\n"
        "• *hoy\n"
        "• *mañana\n"
        "• dd/mm/aaaa\n\n"
        "Ejemplos:\n"
        "• Comprar pilas @Casa *hoy\n"
        "• Llamar al dentista 18/03/2026\n\n"
        "Comandos:\n"
        "/nextactions\n"
        "/hoy"
    )
    await update.message.reply_text(msg)


async def cmd_nextactions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = q(
        "SELECT t.id, t.title, t.due_date "
        "FROM tasks t "
        "JOIN task_tags tt ON tt.task_id=t.id "
        "JOIN tags tg ON tg.id=tt.tag_id "
        "WHERE t.completed_at IS NULL "
        "AND LOWER(tg.name)=LOWER(%s) "
        "ORDER BY (t.due_date IS NULL) ASC, t.due_date ASC, t.id DESC",
        ("NextAction",),
    )

    if not rows:
        await update.message.reply_text("No hay siguientes acciones.")
        return

    text = "NextActions:\n\n" + "\n".join(format_task_line(r) for r in rows[:30])
    if len(rows) > 30:
        text += f"\n\n… y {len(rows) - 30} más."

    await update.message.reply_text(text)


async def cmd_hoy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    today_d = today_local()

    rows = q(
        "SELECT id, title, due_date "
        "FROM tasks "
        "WHERE completed_at IS NULL AND due_date=%s "
        "ORDER BY id DESC",
        (today_d,),
    )

    if not rows:
        await update.message.reply_text("No hay tareas para hoy.")
        return

    text = "Tareas para hoy:\n\n" + "\n".join(format_task_line(r) for r in rows[:30])
    if len(rows) > 30:
        text += f"\n\n… y {len(rows) - 30} más."

    await update.message.reply_text(text)


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    raw = update.message.text.strip()
    if not raw:
        await update.message.reply_text("Mensaje vacío.")
        return

    # Ignorar comandos desconocidos aquí
    if raw.startswith("/"):
        return

    try:
        task_id, title, due_date, tags = create_inbox_task_from_text(raw)

        parts = [f"✅ Tarea creada en Inbox: {title}"]
        if due_date:
            parts.append(f"📅 Fecha: {due_date.strftime('%d/%m/%Y')}")
        if tags:
            parts.append("🏷 Etiquetas: " + ", ".join(tags))
        parts.append(f"🆔 ID: {task_id}")

        await update.message.reply_text("\n".join(parts))

    except ValueError as e:
        await update.message.reply_text(f"⚠️ {e}")
    except Exception as e:
        await update.message.reply_text(f"❌ No se pudo crear la tarea: {e}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        print("Telegram bot error:", context.error)
    except Exception:
        pass


# =========================================================
# Main
# =========================================================
def main() -> None:
    token = load_bot_token()
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("nextactions", cmd_nextactions))
    app.add_handler(CommandHandler("hoy", cmd_hoy))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    app.add_error_handler(error_handler)

    print("GtdAppBot arrancado.")
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
