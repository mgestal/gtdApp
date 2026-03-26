from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pymysql
from telegram import Update
from telegram.constants import ChatType
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters



# =========================================================
# Paths
# =========================================================
BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
CONFIG_PATH = INSTANCE_DIR / "config.json"


# =========================================================
# Configuración
# =========================================================
def load_config() -> Dict[str, Any]:

    if not CONFIG_PATH.exists():
        raise RuntimeError(f"No existe el fichero de configuración: {CONFIG_PATH}")

    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        raise RuntimeError(f"No se pudo leer config.json: {e}")

    if not isinstance(data, dict):
        raise RuntimeError("config.json no contiene un objeto JSON válido")

    return data


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

    token_path = BASE_DIR / ".token-gtdapp"

    if not token_path.exists():
        raise RuntimeError("No existe el fichero .token-gtdapp con el token del bot")

    token = token_path.read_text(encoding="utf-8").strip()

    if not token:
        raise RuntimeError("El fichero .token-gtdapp está vacío")

    return token


# =========================================================
# Debug
# =========================================================

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await reject_if_unauthorized(update):
        return

    if not update.message or not update.message.text:
        print("sin message o sin text")
        return

    raw = update.message.text.strip()
    print(f"mensaje recibido: {raw!r}")


# =========================================================
# Seguridad
# =========================================================


def security_cfg() -> Dict[str, Any]:
    return load_config().get("security", {})


def allowed_user_ids() -> set[int]:

    raw = security_cfg().get("allowed_user_ids", [])
    result = set()

    for x in raw:
        try:
            result.add(int(x))
        except Exception:
            pass

    return result


def allowed_group_ids() -> set[int]:

    raw = security_cfg().get("allowed_group_ids", [])
    result = set()

    for x in raw:
        try:
            result.add(int(x))
        except Exception:
            pass

    return result


def reply_on_unauthorized() -> bool:
    return bool(security_cfg().get("reply_on_unauthorized", True))


def authorization_error(update: Update) -> Optional[str]:
    user = update.effective_user
    chat = update.effective_chat

    print(
        "DEBUG AUTH:",
        {
            "user_id": user.id if user else None,
            "chat_id": chat.id if chat else None,
            "chat_type": chat.type if chat else None,
            "allowed_user_ids": sorted(allowed_user_ids()),
            "allowed_group_ids": sorted(allowed_group_ids()),
        }
    )

    if not user or not chat:
        return "Acceso no autorizado."

    user_id = int(user.id)
    chat_id = int(chat.id)

    users = allowed_user_ids()
    groups = allowed_group_ids()

    if chat.type == ChatType.PRIVATE:
        if user_id not in users:
            return "Usuario no autorizado."
        return None

    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        if chat_id not in groups:
            return "Grupo no autorizado."
        if user_id not in users:
            return "Usuario no autorizado en este grupo."
        return None

    return "Tipo de chat no autorizado."


async def reject_if_unauthorized(update: Update) -> bool:
    msg = authorization_error(update)

    if msg is None:
        return False

    print(f"rechazado: {msg}")

    if reply_on_unauthorized():
        try:
            await update.get_bot().send_message(
                chat_id=update.effective_chat.id,
                text=msg,
            )
        except Exception as e:
            print(f"No se pudo enviar mensaje de no autorizado: {e}")

    return True

# =========================================================
# Validación de configuración
# =========================================================
def validate_config():

    cfg = load_config()

    db = cfg.get("db", {})

    required = ["host", "port", "user", "password", "database"]

    missing = [k for k in required if not db.get(k)]

    if missing:
        raise RuntimeError(
            f"Faltan campos de DB en config.json: {', '.join(missing)}"
        )

    users = allowed_user_ids()
    groups = allowed_group_ids()

    if not users and not groups:
        raise RuntimeError(
            "Configuración insegura: define security.allowed_user_ids "
            "o security.allowed_group_ids en config.json"
        )


# =========================================================
# DB
# =========================================================
def get_db_conn():

    cfg = load_config()
    db = cfg["db"]

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


# =========================================================
# Parsing de tareas
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

        return datetime.strptime(
            f"{day:02d}/{month:02d}/{year}",
            "%d/%m/%Y"
        ).date()

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

    tags = [normalize_name(t) for t in TAG_RE.findall(cleaned)]

    title = TAG_RE.sub("", cleaned)

    title = re.sub(r"\s+", " ", title).strip(" -_,.;:")

    return title, tags, due_date


# =========================================================
# Crear tarea
# =========================================================
def create_inbox_task_from_text(raw_text: str):

    title, tags, due_date = parse_task_text(raw_text)

    if not title:
        raise ValueError("No se detectó un título válido")

    conn = get_db_conn()

    try:

        with conn.cursor() as cur:

            cur.execute(
                "INSERT INTO tasks(title, project_id, folder_id, due_date, recurrence_rule) "
                "VALUES(%s,NULL,NULL,%s,NULL)",
                (title, due_date),
            )

            task_id = cur.lastrowid or 0

            # Añadir automáticamente la etiqueta telegrambot
            all_tags = list(tags)
            if "telegrambot" not in [t.lower() for t in all_tags]:
                all_tags.append("telegrambot")

            for tag_name in all_tags:

                cur.execute("SELECT id FROM tags WHERE name=%s", (tag_name,))
                row = cur.fetchone()

                if row:
                    tag_id = int(row["id"])
                else:
                    cur.execute("INSERT INTO tags(name) VALUES(%s)", (tag_name,))
                    tag_id = cur.lastrowid or 0

                cur.execute(
                    "INSERT IGNORE INTO task_tags(task_id,tag_id) VALUES(%s,%s)",
                    (task_id, tag_id),
                )

        conn.commit()

        return task_id, title, due_date, all_tags

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

def _fmt_due_time(due_time: Any) -> Optional[str]:
    if due_time is None:
        return None
    if isinstance(due_time, timedelta):
        total_seconds = int(due_time.total_seconds()) % (24 * 3600)
        hh = total_seconds // 3600
        mm = (total_seconds % 3600) // 60
        return f"{hh:02d}:{mm:02d}"
    if hasattr(due_time, "strftime"):
        try:
            return due_time.strftime("%H:%M")
        except Exception:
            return None
    return None


def format_task_extended(row, show_date: bool = True):
    """
    Formato Telegram:
    • Título
      📁 Proyecto o carpeta
      🏷 @tag1 @tag2
      📅 dd/mm/aaaa
    """
    title = (row.get("title") or "").strip() or "(sin título)"
    project_name = (row.get("project_name") or "").strip()
    folder_name = (row.get("folder_name") or "").strip()
    tags = row.get("tags") or []
    due_date = row.get("due_date")
    due_time = row.get("due_time")

    lines = [f"• {title}"]

    # Mostrar solo proyecto; si no existe, carpeta
    if project_name:
        lines.append(f"  💼 {project_name}")
    elif folder_name:
        lines.append(f"  📂 {folder_name}")
    else:
        lines.append("  📥 Inbox")

    if tags:
        tag_text = " ".join(f"@{t}" for t in tags)
        lines.append(f"  🏷 {tag_text}")

    if show_date and due_date:
        lines.append(f"  📅 {due_date.strftime('%d/%m/%Y')}")

    hhmm = _fmt_due_time(due_time)
    if hhmm:
        lines.append(f"  🕒 {hhmm}")

    return "\n".join(lines)



def get_today_tasks_extended():
    """
    Devuelve tareas pendientes para hoy con contexto:
    - project_name
    - folder_name
    - tags
    """
    conn = get_db_conn()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    t.id,
                    t.title,
                    t.due_date,
                                        t.due_time,
                    p.name AS project_name,
                    fd.name AS folder_name
                FROM tasks t
                LEFT JOIN projects p ON p.id = t.project_id
                LEFT JOIN folders fd ON fd.id = COALESCE(t.folder_id, p.folder_id)
                WHERE t.completed_at IS NULL
                                    AND t.deleted_at IS NULL
                                    AND COALESCE(t.archived, 0) = 0
                                    AND (t.project_id IS NULL OR COALESCE(p.archived, 0) = 0)
                  AND t.due_date = CURDATE()
                ORDER BY t.id ASC
                """
            )
            rows = list(cur.fetchall())

            task_ids = [int(r["id"]) for r in rows]

            tags_map = {}
            if task_ids:
                placeholders = ",".join(["%s"] * len(task_ids))
                cur.execute(
                    f"""
                    SELECT
                        tt.task_id,
                        tg.name
                    FROM task_tags tt
                    JOIN tags tg ON tg.id = tt.tag_id
                    WHERE tt.task_id IN ({placeholders})
                    ORDER BY tg.name
                    """,
                    tuple(task_ids),
                )
                for r in cur.fetchall():
                    tags_map.setdefault(int(r["task_id"]), []).append(r["name"])

            for row in rows:
                row["tags"] = tags_map.get(int(row["id"]), [])

            return rows

    finally:
        conn.close()


def get_agenda_tasks_extended(limit: int = 15):
    """
    Devuelve próximas tareas pendientes con fecha, ordenadas por due_date.
    """
    conn = get_db_conn()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    t.id,
                    t.title,
                    t.due_date,
                                        t.due_time,
                    p.name AS project_name,
                    fd.name AS folder_name
                FROM tasks t
                LEFT JOIN projects p ON p.id = t.project_id
                LEFT JOIN folders fd ON fd.id = COALESCE(t.folder_id, p.folder_id)
                WHERE t.completed_at IS NULL
                                    AND t.deleted_at IS NULL
                                    AND COALESCE(t.archived, 0) = 0
                                    AND (t.project_id IS NULL OR COALESCE(p.archived, 0) = 0)
                  AND t.due_date IS NOT NULL
                ORDER BY t.due_date ASC, t.id ASC
                LIMIT %s
                """,
                (limit,),
            )
            rows = list(cur.fetchall())

            task_ids = [int(r["id"]) for r in rows]

            tags_map = {}
            if task_ids:
                placeholders = ",".join(["%s"] * len(task_ids))
                cur.execute(
                    f"""
                    SELECT
                        tt.task_id,
                        tg.name
                    FROM task_tags tt
                    JOIN tags tg ON tg.id = tt.tag_id
                    WHERE tt.task_id IN ({placeholders})
                    ORDER BY tg.name
                    """,
                    tuple(task_ids),
                )
                for r in cur.fetchall():
                    tags_map.setdefault(int(r["task_id"]), []).append(r["name"])

            for row in rows:
                row["tags"] = tags_map.get(int(row["id"]), [])

            return rows

    finally:
        conn.close()

async def cmd_agenda(update, context):
    rows = get_agenda_tasks_extended(limit=15)

    if not rows:
        await update.message.reply_text("📅 No hay tareas en agenda.")
        return

    text = "📅 Agenda\n\n" + "\n\n".join(format_task_extended(r) for r in rows)

    await update.message.reply_text(text)

# =========================================================
# Comandos
# =========================================================

async def cmd_today(update, context):
    if await reject_if_unauthorized(update):
        return

    rows = get_today_tasks_extended()

    if not rows:
        await update.message.reply_text("📅 Hoy no tienes tareas.")
        return

    text = "📅 Hoy\n\n" + "\n\n".join(format_task_extended(r, show_date=False) for r in rows[:20])

    if len(rows) > 20:
        text += f"\n\n… y {len(rows) - 20} más."

    await update.message.reply_text(text)



async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if await reject_if_unauthorized(update):
        return

    msg = (
        "GtdAppBot listo.\n\n"
        "Envía un mensaje y lo crearé como tarea en Inbox.\n\n"
        "Formato:\n"
        "• tarea @etiqueta\n"
        "• *hoy\n"
        "• *mañana\n"
        "• dd/mm/aaaa\n\n"
        "Comandos:\n"
        "/nextactions\n"
        "/hoy"
    )

    await update.message.reply_text(msg)

async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    user = update.effective_user
    chat = update.effective_chat

    await update.message.reply_text(
        "\n".join(
            [
                f"user_id={user.id if user else 'None'}",
                f"chat_id={chat.id if chat else 'None'}",
                f"chat_type={chat.type if chat else 'None'}",
            ]
        )
    )

async def cmd_nextactions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("cmd_nextactions llamado")

    if await reject_if_unauthorized(update):
        print("cmd_nextactions rechazado")
        return

    rows = q(
        "SELECT t.id,t.title,t.due_date "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "JOIN task_tags tt ON tt.task_id=t.id "
        "JOIN tags tg ON tg.id=tt.tag_id "
        "WHERE t.completed_at IS NULL "
        "AND t.deleted_at IS NULL "
        "AND COALESCE(t.archived,0)=0 "
        "AND (t.project_id IS NULL OR COALESCE(p.archived,0)=0) "
        "AND LOWER(tg.name)=LOWER(%s) "
        "ORDER BY (t.due_date IS NULL), t.due_date, t.id DESC",
        ("NextAction",),
    )

    if not rows:

        await update.message.reply_text("No hay siguientes acciones.")

        return

    text = "NextActions:\n\n" + "\n".join(format_task_line(r) for r in rows[:30])

    await update.message.reply_text(text)


async def cmd_hoy(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if await reject_if_unauthorized(update):
        return

    today_d = today_local()

    rows = q(
        "SELECT t.id,t.title,t.due_date "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE t.completed_at IS NULL "
        "AND t.deleted_at IS NULL "
        "AND COALESCE(t.archived,0)=0 "
        "AND (t.project_id IS NULL OR COALESCE(p.archived,0)=0) "
        "AND t.due_date=%s",
        (today_d,),
    )

    if not rows:

        await update.message.reply_text("No hay tareas para hoy.")

        return

    text = "Tareas para hoy:\n\n" + "\n".join(format_task_line(r) for r in rows[:30])

    await update.message.reply_text(text)


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if await reject_if_unauthorized(update):
        return

    if not update.message or not update.message.text:
        return

    raw = update.message.text.strip()

    if not raw:
        await update.message.reply_text("Mensaje vacío.")
        return

    try:

        task_id, title, due_date, tags = create_inbox_task_from_text(raw)

        parts = [f"✅ Tarea creada: {title}"]

        if due_date:
            parts.append(f"📅 {due_date.strftime('%d/%m/%Y')}")

        if tags:
            parts.append("🏷 " + ", ".join(tags))

        parts.append(f"ID {task_id}")

        await update.message.reply_text("\n".join(parts))

    except ValueError as e:

        await update.message.reply_text(str(e))

    except Exception as e:

        await update.message.reply_text(f"Error creando tarea: {e}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("Telegram bot error:", repr(context.error))

def bot_help_text() -> str:
    return (
        "GtdAppBot\n\n"
        "Envía un mensaje y lo crearé como tarea en Inbox.\n\n"
        "Formato soportado:\n"
        "• tarea @etiqueta  [*hoy  *mañana dd/mm/aaaa] \n"
        " Ejemplos:\n"
        "• Comprar pilas @Casa *hoy\n"
        "• Llamar al dentista 18/03/2026\n\n"
        "Comandos válidos:\n"
        "/start - mensaje de bienvenida\n"
        "/help - muestra esta ayuda\n"
        "/info - muestra información del bot\n"
        "/nextactions - lista tareas con etiqueta NextAction\n"
        "/hoy - lista tareas con fecha de hoy\n"
        "/today - lista tareas con fecha de hoy (formato extendido)\n"
        "/agenda - lista tareas en agenda\n"
        "/done - lista tareas completadas hoy\n"
        "/whoami - muestra tu user_id y chat_id"
    )


def format_done_task_line(row: Dict[str, Any]) -> str:
    title = row.get("title", "").strip() or "(sin título)"
    completed_at = row.get("completed_at")

    if completed_at:
        return f"• ({completed_at.strftime('%H:%M')}) - {title}"

    return f"• {title}"

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("cmd_start llamado")

    if await reject_if_unauthorized(update):
        print("cmd_start rechazado")
        return

    if update.message:
        await update.message.reply_text(bot_help_text())


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("cmd_help llamado")

    if await reject_if_unauthorized(update):
        print("cmd_help rechazado")
        return

    if update.message:
        await update.message.reply_text(bot_help_text())


async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("cmd_info llamado")

    if await reject_if_unauthorized(update):
        print("cmd_info rechazado")
        return

    if update.message:
        await update.message.reply_text(bot_help_text())


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("cmd_done llamado")

    if await reject_if_unauthorized(update):
        print("cmd_done rechazado")
        return

    today_d = today_local()

    rows = q(
        "SELECT t.id, t.title, t.completed_at "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "WHERE t.completed_at IS NOT NULL "
        "AND t.deleted_at IS NULL "
        "AND COALESCE(t.archived,0)=0 "
        "AND (t.project_id IS NULL OR COALESCE(p.archived,0)=0) "
        "AND DATE(t.completed_at)=%s "
        "ORDER BY t.completed_at DESC, t.id DESC",
        (today_d,),
    )

    if not rows:
        if update.message:
            await update.message.reply_text("No hay tareas completadas hoy.")
        return

    text = "Tareas completadas hoy:\n\n" + "\n".join(
        format_done_task_line(r) for r in rows[:30]
    )

    if len(rows) > 30:
        text += f"\n\n… y {len(rows) - 30} más."

    if update.message:
        await update.message.reply_text(text)



def get_today_tasks():
    """
    Devuelve las tareas con due_date = hoy y no completadas
    """
    conn = pymysql.connect(
        host="localhost",
        user="gtd",
        password="gtd_password",
        database="gtd",
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor
    )

    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, title
            FROM tasks t
            LEFT JOIN projects p ON p.id=t.project_id
            WHERE t.due_date = CURDATE()
            AND t.completed_at IS NULL
            AND t.deleted_at IS NULL
            AND COALESCE(t.archived,0)=0
            AND (t.project_id IS NULL OR COALESCE(p.archived,0)=0)
            ORDER BY t.id
        """)
        rows = cur.fetchall()

    conn.close()

    return rows


async def send_today_summary():
    """
    Envía la lista de tareas de hoy al chat configurado.
    """
    from telegram import Bot
    import datetime

    token = load_bot_token()
    bot = Bot(token)

    chat_id = load_chat_id()   # veremos esto abajo

    today_tasks = get_today_tasks()  # tu función existente

    text = "================================= \n"
    text += f"  Tareas para hoy ({datetime.date.today().strftime('%d/%m/%Y')}):\n"
    text += "================================= \n\n"

    if not today_tasks:
        text += "📅 Hoy no tienes tareas."
    else:
        text += "📅 Tareas para hoy:\n\n"
        for t in today_tasks:
            text += f"• {t['title']}\n"

    text += "\n\n¡Que tengas un gran día! 🚀 \n\n"

    await bot.send_message(chat_id=chat_id, text=text)


def save_chat_id(chat_id):
    path = BASE_DIR / "instance" / "telegram_chat.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"chat_id": chat_id}), encoding="utf-8")


async def cmd_start(update, context):
    save_chat_id(update.effective_chat.id)
    await update.message.reply_text("Bot configurado 👍")

def load_chat_id():
    path = BASE_DIR / "instance" / "telegram_chat.json"
    return json.loads(path.read_text())["chat_id"]

    
    

# =========================================================
# Main
# =========================================================
def main():

    validate_config()

    token = load_bot_token()

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("info", cmd_info))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("nextactions", cmd_nextactions))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("hoy", cmd_hoy))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("agenda", cmd_agenda))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    app.add_error_handler(error_handler)

    print("GtdAppBot arrancado.")

    app.run_polling(drop_pending_updates=False)





def main_debug() -> None:
    print("Iniciando bot...")
    validate_config()
    print("Config OK")

    token = load_bot_token()
    print("Token OK")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("info", cmd_info))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("nextactions", cmd_nextactions))
    app.add_handler(CommandHandler("done", cmd_done))   
    app.add_handler(CommandHandler("hoy", cmd_hoy))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("agenda", cmd_agenda))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))

    app.add_error_handler(error_handler)

    print("GtdAppBot arrancado.")
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()