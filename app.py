from __future__ import annotations

import subprocess
import json
import os
import re
import calendar
import pymysql


from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for, send_file, jsonify

import io
import csv
from xml.etree import ElementTree as ET
from werkzeug.utils import secure_filename

from gmail_import import (
    build_gmail_service,
    list_matching_messages,
    get_message_metadata,
    message_to_task_payload,
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


def get_page_arg(name: str = "page", default: int = 1) -> int:
    """Devuelve page desde query string con protección contra valores inválidos."""
    return coerce_int(request.args.get(name, default), default, min_v=1)


def get_pagination(total: int, per_page: int, page: int) -> Tuple[int, int, int]:
    """Calcula page, pages y offset para paginación."""
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(page, 1), pages)
    offset = (page - 1) * per_page
    return page, pages, offset


def safe_next_url(next_url: Optional[str], fallback_endpoint: str = "home", **fallback_values) -> str:
    """Evita open redirect y normaliza rutas bajo script_root."""
    fallback = url_for(fallback_endpoint, **fallback_values)
    next_url = (next_url or "").strip()
    if not next_url:
        return fallback

    parsed = urlparse(next_url)
    if parsed.scheme or parsed.netloc:
        return fallback

    script_root = (request.script_root or "").rstrip("/")
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

subdb = SubDB(q=q, q1=q1, exec_sql=exec_sql, commit=commit, rollback=rollback)
register_subtask_routes(app, subdb)

# -----------------------------------------------
# ---------------- Parsing quick entry ----------------
# -----------------------------------------------

TAG_RE = re.compile(r"@([A-Za-z0-9_\-áéíóúÁÉÍÓÚñÑ]+)")
PROJ_RE = re.compile(r"#([A-Za-z0-9_\-áéíóúÁÉÍÓÚñÑ][A-Za-z0-9_\- áéíóúÁÉÍÓÚñÑ]*)")

def normalize_name(s: str) -> str:
    return (s or "").strip()
    
    
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

    # 2) Si no hay *, buscar fecha "bare" (sin prefijo)
    m = DATE_BARE_RE.search(s)
    if m:
        token = m.group(1)
        due_date = parse_due_token(token)
        cleaned = (s[:m.start()] + " " + s[m.end():]).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        return due_date, cleaned

    return None, s


def parse_task_quick_entry(raw_title: str) -> Tuple[str, List[str], Optional[str]]:
    tags = TAG_RE.findall(raw_title or "")
    m = PROJ_RE.search(raw_title or "")
    project_name = m.group(1).strip() if m else None

    title = TAG_RE.sub("", raw_title)
    title = PROJ_RE.sub("", title)
    title = re.sub(r"\s+", " ", title).strip()

    tags = [normalize_name(t) for t in tags if normalize_name(t)]
    if project_name:
        project_name = normalize_name(project_name)
    return title, tags, project_name
    
  
    

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

def parse_tags_csv(tags_csv: str) -> List[str]:
    # Se aceptan etiquetas separadas por comas con prefijo @ (p.e. "@NextAction, @Casa").
    s = (tags_csv or "").strip()
    if not s:
        return []

    parts = [p.strip() for p in s.split(",") if p.strip()]
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
    #   - prefijos como p:proyecto, f:carpeta, fr:carpeta-recursiva, fa:carpeta-anywhere pf:proyectORfolder
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
    # integer days from today
    if re.fullmatch(r"-?\d+", r):
        return _today_madrid() + timedelta(days=int(r)), False
    # dd-mm-aaaa
    try:
        return datetime.strptime(r, "%d-%m-%Y").date(), False
    except ValueError:
        raise FilterParseError(f"Referencia de fecha inválida: '{ref}'. Usa hoy, NULL, N o dd-mm-aaaa.")



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
                        "(t.folder_id IN ("
                        "WITH RECURSIVE subfolders AS ("
                        " SELECT id FROM folders WHERE name=%s"
                        " UNION ALL"
                        " SELECT f.id FROM folders f"
                        " JOIN subfolders sf ON f.parent_id = sf.id"
                        ")"
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
                        "t.folder_id IN ("
                        "WITH RECURSIVE subfolders AS ("
                        " SELECT id FROM folders WHERE name=%s"
                        " UNION ALL"
                        " SELECT f.id FROM folders f"
                        " JOIN subfolders sf ON f.parent_id = sf.id"
                        ") "
                        "SELECT id FROM subfolders"
                        ") "
                        "OR "
                        "p.folder_id IN ("
                        "WITH RECURSIVE subfolders AS ("
                        " SELECT id FROM folders WHERE name=%s"
                        " UNION ALL"
                        " SELECT f.id FROM folders f"
                        " JOIN subfolders sf ON f.parent_id = sf.id"
                        ") "
                        "SELECT id FROM subfolders"
                        ")"
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


@app.route("/search")
def search():
    qtxt = (request.args.get("q") or "").strip()

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

    if qtxt:
        # total resultados (para paginar)
        total_row = q1(
            "SELECT COUNT(*) AS c "
            "FROM tasks t "
            "WHERE MATCH(t.title, t.notes) AGAINST(%s IN BOOLEAN MODE)",
            (qtxt + "*",),
        )
        total = int(total_row["c"]) if total_row else 0
        pages = max(1, (total + per_page - 1) // per_page)

        rows = q(
            "SELECT t.id, t.title, t.notes, t.due_date, t.completed_at, t.recurrence_rule, "
            "p.name AS project_name, p.id AS project_id, "
            "fd.id AS folder_id, fd.name AS folder_name "
            "FROM tasks t "
            "LEFT JOIN projects p ON p.id=t.project_id "
            "LEFT JOIN folders fd ON fd.id = COALESCE(t.folder_id, p.folder_id) "
            "WHERE MATCH(t.title, t.notes) AGAINST(%s IN BOOLEAN MODE) "
            "ORDER BY (t.completed_at IS NOT NULL) ASC, "
            "(t.due_date IS NULL) ASC, t.due_date ASC, t.id DESC "
            "LIMIT %s OFFSET %s",
            (qtxt + "*", per_page, offset),
        )

    tags_map = load_tags_map([r["id"] for r in rows]) if rows else {}
    return render_template(
        "search.html",
        qtxt=qtxt,
        rows=rows,
        tags_map=tags_map,
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
        "all_projects": q("SELECT id, name FROM projects WHERE archived=0 ORDER BY name"),
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
                "WHERE t.completed_at IS NULL "
                "AND tt.tag_id=%s",
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
                "FROM tasks "
                "WHERE completed_at IS NULL AND due_date=%s",
                (today_d,)
            ) or {}).get("c", 0)),

            "week": int((q1(
                "SELECT COUNT(*) AS c "
                "FROM tasks "
                "WHERE completed_at IS NULL "
                "AND due_date IS NOT NULL "
                "AND due_date >= %s "
                "AND due_date <= %s",
                (today_d, sunday_d)
            ) or {}).get("c", 0)),

            "next": next_count,

            "agenda": int((q1(
                "SELECT COUNT(*) AS c "
                "FROM tasks "
                "WHERE completed_at IS NULL AND due_date IS NOT NULL"
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
        "SELECT t.id, t.title, t.notes, t.due_date, t.completed_at, t.recurrence_rule "
        "FROM tasks t "
        "WHERE t.project_id IS NULL AND t.folder_id IS NULL "
        "ORDER BY (t.due_date IS NULL), t.due_date ASC, t.id DESC "
        "LIMIT 200"
    )

    # proyectos sin carpeta
    orphan_projects = q(
        "SELECT id, name, archived "
        "FROM projects "
        "WHERE folder_id IS NULL "
        "ORDER BY archived ASC, name ASC"
    )

    tags_map = load_tags_map([r["id"] for r in inbox])

    return render_template(
        "home.html",
        inbox=inbox,
        tags_map=tags_map,
        orphan_projects=orphan_projects
    )

@app.route("/agenda")
def agenda():

    per_page = cfg_int(["app", "pagination", "agenda_per_page"], default=25, min_v=5, max_v=500)

    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    page = max(page, 1)
    offset = (page - 1) * per_page

    # Total de tareas con fecha (si tu agenda incluye solo tareas con due_date)
    total_row = q1(
        "SELECT COUNT(*) AS c FROM tasks WHERE due_date IS NOT NULL AND completed_at IS NULL"
    )
    total = int(total_row["c"]) if total_row else 0
    pages = max(1, (total + per_page - 1) // per_page)

    rows = q(
        "SELECT t.id, t.title, t.notes, t.due_date, t.completed_at, t.recurrence_rule, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.id AS folder_id, fd.name AS folder_name "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "WHERE t.due_date IS NOT NULL "
        "AND t.completed_at IS NULL "
        "ORDER BY (t.completed_at IS NOT NULL) ASC, t.due_date ASC, t.id DESC "
        "LIMIT %s OFFSET %s",
        (per_page, offset),
    )
    
    
    tags_map = load_tags_map([r["id"] for r in rows])

    return render_template(
        "agenda.html",
        rows=rows,
        tags_map=tags_map,
        page=page,
        pages=pages,
        total=total,
        per_page=per_page,
    )

@app.route("/today")
def today():
    today_d = _today_madrid()

    pending_rows = q(
        "SELECT t.id, t.title, t.notes, t.due_date, t.completed_at, t.recurrence_rule, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.id AS folder_id, fd.name AS folder_name "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "WHERE t.due_date=%s AND t.completed_at IS NULL "
        "ORDER BY t.id DESC",
        (today_d,)
    )

    done_rows = q(
        "SELECT t.id, t.title, t.notes, t.due_date, t.completed_at, t.recurrence_rule, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.id AS folder_id, fd.name AS folder_name "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "WHERE t.completed_at IS NOT NULL AND DATE(t.completed_at)=%s "
        "ORDER BY t.completed_at DESC, t.id DESC",
        (today_d,)
    )

    all_ids = [r["id"] for r in pending_rows] + [r["id"] for r in done_rows]
    tags_map = load_tags_map(all_ids) if all_ids else {}
    return render_template("today.html", pending_rows=pending_rows, done_rows=done_rows, tags_map=tags_map, today=today_d)


@app.route("/week")
def week():

    from datetime import timedelta

    today_d = _today_madrid()

    # lunes de la semana actual
    monday_d = today_d - timedelta(days=today_d.weekday())

    # domingo de la semana actual
    sunday_d = monday_d + timedelta(days=6)

    rows = q(
        "SELECT t.id, t.title, t.notes, t.due_date, t.completed_at, t.recurrence_rule, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id = COALESCE(t.folder_id, p.folder_id) "
        "WHERE t.due_date IS NOT NULL "
        "AND t.due_date >= %s "
        "AND t.due_date <= %s "
        "ORDER BY (t.completed_at IS NOT NULL) ASC, t.due_date ASC, t.id DESC",
        (monday_d, sunday_d)
    )

    tags_map = load_tags_map([r["id"] for r in rows]) if rows else {}

    return render_template(
        "week.html",
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
    if view not in ("day", "week", "month"):
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
    else:
        start_date = selected_date
        end_date = selected_date
        prev_date = selected_date - timedelta(days=1)
        next_date = selected_date + timedelta(days=1)
        week_days = None

    show_completed = str(request.args.get("show_completed", "0")).lower() in ("1", "true", "on", "yes")

    # Pendientes por due_date dentro de rango
    pending_rows = q(
        "SELECT t.id, t.title, t.notes, t.due_date, t.completed_at, t.recurrence_rule, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.id AS folder_id, fd.name AS folder_name "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=COALESCE(t.folder_id, p.folder_id) "
        "WHERE t.due_date IS NOT NULL "
        "AND t.due_date >= %s "
        "AND t.due_date <= %s "
        "AND t.completed_at IS NULL "
        "ORDER BY t.due_date ASC, t.id DESC",
        (start_date, end_date),
    )

    # Completadas según completed_at (independiente due_date)
    completed_rows = q(
        "SELECT t.id, t.title, t.notes, t.due_date, t.completed_at, t.recurrence_rule, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.id AS folder_id, fd.name AS folder_name "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=COALESCE(t.folder_id, p.folder_id) "
        "WHERE t.completed_at IS NOT NULL "
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

    selected_day_tasks = list(selected_day_pending)
    if show_completed:
        pending_ids = {t["id"] for t in selected_day_pending}
        for t in selected_day_completed:
            if t["id"] not in pending_ids:
                selected_day_tasks.append(t)

    tags_map = load_tags_map([t["id"] for t in selected_day_tasks]) if selected_day_tasks else {}
    has_done = len(selected_day_completed)

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
        has_done=has_done,
        tags_map=tags_map,
    )


@app.route("/projects")
def projects():
    qtxt = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "all").strip().lower()

    if status not in ("all", "active", "archived"):
        status = "all"

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

    if status == "active":
        where_parts.append("p.archived=0")
    elif status == "archived":
        where_parts.append("p.archived=1")

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

    active = [r for r in rows if int(r["archived"]) == 0]
    archived = [r for r in rows if int(r["archived"]) == 1]

    folders = q("SELECT id, parent_id, name FROM folders ORDER BY name")

    return render_template(
        "projects.html",
        active=active,
        archived=archived,
        folders=folders,
        qtxt=qtxt,
        status=status,
        page=page,
        pages=pages,
        total=total,
        per_page=per_page,
    )

@app.route("/projects/<int:project_id>")
def project_detail(project_id: int):
    project = q1("SELECT id, name, description, archived FROM projects WHERE id=%s", (project_id,))
    if not project:
        abort(404)

    active_tasks = q(
        "SELECT id, title, notes, due_date, completed_at, recurrence_rule "
        "FROM tasks "
        "WHERE project_id=%s AND completed_at IS NULL "
        "ORDER BY (due_date IS NULL) ASC, due_date ASC, id",
        (project_id,),
    )

    done_tasks = q(
        "SELECT id, title, notes, due_date, completed_at, recurrence_rule "
        "FROM tasks "
        "WHERE project_id=%s AND completed_at IS NOT NULL "
        "ORDER BY completed_at DESC, id",
        (project_id,),
    )

    # tags y subtareas para ambas listas
    all_ids = [t["id"] for t in active_tasks] + [t["id"] for t in done_tasks]
    tags_map = load_tags_map(all_ids) if all_ids else {}
    sub_counts = load_subtask_counts(subdb, all_ids)
    sub_map = load_subtasks_map(subdb, all_ids)

    return render_template(
        "project_detail.html",
        project=project,
        active_tasks=active_tasks,
        done_tasks=done_tasks,
        tags_map=tags_map,
        sub_counts=sub_counts, sub_map=sub_map
    )


@app.route("/projects/<int:project_id>/edit", methods=["GET", "POST"])
def project_edit(project_id: int):
    proj = q1("SELECT id, name, description, folder_id, archived FROM projects WHERE id=%s", (project_id,))
    if not proj:
        abort(404)

    folders = q("SELECT id, parent_id, name FROM folders ORDER BY name")

    if request.method == "POST":
        name = normalize_name(request.form.get("name", ""))
        desc = (request.form.get("description") or "").strip() or None
        folder_raw = request.form.get("folder_id") or ""
        folder_id = int(folder_raw) if folder_raw else None

        if not name:
            flash("El nombre del proyecto es obligatorio.", "error")
            return redirect(url_for("project_edit", project_id=project_id))

        try:
            exec_sql(
                "UPDATE projects SET name=%s, description=%s, folder_id=%s, updated_at=NOW() WHERE id=%s",
                (name, desc, folder_id, project_id),
            )
            commit()
            flash("Proyecto actualizado.", "ok")
            return redirect(url_for("project_detail", project_id=project_id))
        except Exception as e:
            rollback()
            flash(f"No se pudo actualizar el proyecto: {e}", "error")
            return redirect(url_for("project_edit", project_id=project_id))

    return render_template("project_edit.html", project=proj, folders=folders)


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
        exec_sql("UPDATE projects SET archived=0, updated_at=NOW() WHERE id=%s", (project_id,))
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
    proj = q1("SELECT id, name FROM projects WHERE id=%s", (project_id,))
    if not proj:
        abort(404)

    try:
        # 1) Borrar tareas del proyecto (y sus task_tags por cascade)
        exec_sql("DELETE FROM tasks WHERE project_id=%s", (project_id,))

        # 2) Borrar el proyecto
        exec_sql("DELETE FROM projects WHERE id=%s", (project_id,))

        commit()
        flash(f"Proyecto '{proj['name']}' y sus tareas han sido borrados.", "ok")
    except Exception as e:
        rollback()
        flash(f"No se pudo borrar el proyecto: {e}", "error")

    return redirect(url_for("projects"))



@app.route("/tags/<int:tag_id>/edit", methods=["GET", "POST"])
def tag_edit(tag_id: int):
    tag = q1("SELECT id, name FROM tags WHERE id=%s", (tag_id,))
    if not tag:
        abort(404)

    next_url = request.args.get("next") or request.form.get("next") or url_for("tags")

    if request.method == "POST":
        name = normalize_name(request.form.get("name", ""))
        if not name:
            flash("El nombre es obligatorio.", "error")
            return redirect(url_for("tag_edit", tag_id=tag_id, next=next_url))

        try:
            exec_sql("UPDATE tags SET name=%s WHERE id=%s", (name, tag_id))
            commit()
            flash("Etiqueta actualizada.", "ok")
            return redirect(next_url)
        except Exception as e:
            rollback()
            flash(f"No se pudo actualizar: {e}", "error")
            return redirect(url_for("tag_edit", tag_id=tag_id, next=next_url))

    return render_template("tag_edit.html", tag=tag, next_url=next_url)  
    
    
    
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

    per_page = cfg_int(["app", "pagination", "tags_per_page"], default=25, min_v=5, max_v=500)

    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    page = max(1, page)
    offset = (page - 1) * per_page

    params = []
    where = ""
    if qtxt:
        where = "WHERE LOWER(tg.name) LIKE %s"
        params.append(f"%{qtxt.lower()}%")

    total_row = q1(f"SELECT COUNT(*) AS c FROM tags tg {where}", tuple(params))
    total = int(total_row["c"]) if total_row else 0
    pages = max(1, (total + per_page - 1) // per_page)

    if page > pages:
        page = pages
        offset = (page - 1) * per_page

    # (opcional pero útil) contador de tareas por etiqueta
    rows = q(
        "SELECT tg.id, tg.name, COUNT(tt.task_id) AS task_count "
        "FROM tags tg "
        "LEFT JOIN task_tags tt ON tt.tag_id=tg.id "
        f"{where} "
        "GROUP BY tg.id, tg.name "
        "ORDER BY tg.name "
        "LIMIT %s OFFSET %s",
        tuple(params + [per_page, offset]),
    )

    return render_template(
        "tags.html",
        rows=rows,
        qtxt=qtxt,
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
        "WHERE tt.tag_id=%s",
        (tag_id,),
    )
    total = int(total_row["c"]) if total_row else 0
    pages = max(1, (total + per_page - 1) // per_page)

    # tareas (con proyecto para mostrarlo/enlazarlo)
    rows = q(
        "SELECT t.id, t.title, t.notes, t.due_date, t.completed_at, t.recurrence_rule, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.id AS folder_id, fd.name AS folder_name "
        "FROM task_tags tt "
        "JOIN tasks t ON t.id=tt.task_id "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "WHERE tt.tag_id=%s "
        "ORDER BY (t.completed_at IS NOT NULL) ASC, (t.due_date IS NULL) ASC, t.due_date ASC, t.id DESC "
        "LIMIT %s OFFSET %s",
        (tag_id, per_page, offset),
    )

    tags_map = load_tags_map([r["id"] for r in rows]) if rows else {}

    return render_template(
        "tag_detail.html",
        tag=tag,
        rows=rows,
        tags_map=tags_map,
        page=page,
        pages=pages,
        total=total,
        per_page=per_page,
    )
    

from datetime import date, timedelta

@app.route("/dashboard")
def dashboard():
    today = _today_madrid()
    # Lunes de la semana actual
    monday = today - timedelta(days=today.weekday())
    # Primer día del mes actual
    first_of_month = today.replace(day=1)

    # Estadísticas básicas existentes
    total = q1("SELECT COUNT(*) AS c FROM tasks")["c"]
    open_tasks = q1("SELECT COUNT(*) AS c FROM tasks WHERE completed_at IS NULL")["c"]
    completed = q1("SELECT COUNT(*) AS c FROM tasks WHERE completed_at IS NOT NULL")["c"]
    inbox = q1("SELECT COUNT(*) AS c FROM tasks WHERE project_id IS NULL AND completed_at IS NULL")["c"]
    projects_cnt = q1("SELECT COUNT(*) AS c FROM projects WHERE archived=0")["c"]
    archived_cnt = q1("SELECT COUNT(*) AS c FROM projects WHERE archived=1")["c"]

    # --- NUEVAS ESTADÍSTICAS DE COMPLETADOS ---
    comp_today = q1("SELECT COUNT(*) AS c FROM tasks WHERE DATE(completed_at) = %s", (today,))["c"]
    comp_week = q1("SELECT COUNT(*) AS c FROM tasks WHERE DATE(completed_at) >= %s", (monday,))["c"]
    comp_month = q1("SELECT COUNT(*) AS c FROM tasks WHERE DATE(completed_at) >= %s", (first_of_month,))["c"]

    # --- CONSULTA MEJORADA DE VENCIMIENTOS (CON UBICACIÓN) ---
    due_soon = q(
        """
        SELECT t.id, t.title, t.notes, t.due_date, t.project_id, t.folder_id, 
               p.name AS project_name, f.name AS folder_name
        FROM tasks t
        LEFT JOIN projects p ON t.project_id = p.id
        LEFT JOIN folders f ON t.folder_id = f.id
        WHERE t.completed_at IS NULL 
          AND t.due_date IS NOT NULL 
          AND t.due_date <= %s 
        ORDER BY t.due_date ASC 
        """,
        (today + timedelta(days=7),)
    )

    return render_template(
        "dashboard.html",
        stats={
            "total": total,
            "open": open_tasks,
            "completed": completed,
            "inbox": inbox,
            "projects": projects_cnt,
            "archived": archived_cnt,
            "comp_today": comp_today,
            "comp_week": comp_week,
            "comp_month": comp_month,
        },
        due_soon=due_soon,
    )

@app.route("/import")
def import_view():
    return render_template("import.html")


# ---------------- Routes: create/edit tasks ----------------

@app.route("/tasks/create", methods=["POST"])
def task_create():
    import re

    raw = (request.form.get("quick") or "").strip()
    due = request.form.get("due_date") or None
    recurrence = (request.form.get("recurrence_rule") or "").strip() or None

    if not raw:
        flash("El nombre de la tarea es obligatorio.", "error")
        return redirect(request.referrer or url_for("home"))

    raw_work = raw

    # 1) Extraer etiquetas tipo @etiqueta
    tags = re.findall(r'@([^\s@#]+)', raw_work)

    # 2) Extraer fecha desde texto rápido
    detected_due_date = None
    try:
        if not due:
            detected_due_date, raw_work = extract_due_date_from_quick(raw_work)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(request.referrer or url_for("home"))

    # 3) Extraer recurrencia desde texto natural
    if not recurrence:
        for pattern, rule in RECURRENCE_PATTERNS.items():
            if re.search(pattern, raw_work, flags=re.IGNORECASE):
                recurrence = rule
                break

    # 4) Extraer posible proyecto desde #texto, excluyendo la fecha
    project_name = None
    project_candidates = re.findall(r'#([^\s#]+)', raw_work)
    for candidate in project_candidates:
        if re.fullmatch(r'\d{2}-\d{2}-\d{4}', candidate):
            continue
        project_name = candidate.strip()
        break

    # 5) Limpiar el texto para obtener el título real
    title = raw_work
    title = re.sub(r'@([^\s@#]+)', '', title)
    # title = re.sub(r'#\d{2}-\d{2}-\d{4}\b', '', title)
    title = re.sub(r'\bcada\s+dia\b', '', title, flags=re.IGNORECASE)
    title = re.sub(r'\bcada\s+semana\b', '', title, flags=re.IGNORECASE)
    title = re.sub(r'\bcada\s+mes\b', '', title, flags=re.IGNORECASE)
    title = re.sub(r'\bcada\s+año\b', '', title, flags=re.IGNORECASE)
    title = re.sub(r'#([^\s#]+)', '', title)
    title = re.sub(r'\s+', ' ', title).strip(" -_,.;:")

    if not title:
        flash("No se detectó un título válido tras extraer etiquetas, fecha, recurrencia y proyecto.", "error")
        return redirect(request.referrer or url_for("home"))

    # 6) Resolver due_date final
    due_date = None
    if due:
        try:
            due_date = datetime.strptime(due, "%Y-%m-%d").date()
        except ValueError:
            due_date = None
    elif detected_due_date:
        due_date = detected_due_date

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
            # fallback: #Proyecto en texto
            if project_name:
                project_id = find_project_by_name_active(project_name)

                # Si no existe, lo creamos automáticamente
                if project_id is None:
                    project_id = exec_sql(
                        "INSERT INTO projects(name, archived) VALUES(%s, %s)",
                        (project_name, 0),
                    )

        task_id = exec_sql(
            "INSERT INTO tasks(title, project_id, folder_id, due_date, recurrence_rule) VALUES(%s,%s,%s,%s,%s)",
            (title, project_id, folder_id, due_date, recurrence),
        )

        for t in tags:
            tag_id = get_or_create_tag(t)
            exec_sql(
                "INSERT IGNORE INTO task_tags(task_id, tag_id) VALUES(%s,%s)",
                (task_id, tag_id),
            )

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
    if not raw:
        flash("El nombre de la tarea es obligatorio.", "error")
        return redirect(request.form.get("next") or request.referrer or url_for("home"))

    try:
        due_date, raw = extract_due_date_from_quick(raw)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(request.form.get("next") or request.referrer or url_for("home"))

    title, tags, quick_project_name = parse_task_quick_entry(raw)
    
    if not title:
        flash("No se detectó un título válido (deja texto fuera de @etiquetas y #proyecto).", "error")
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
        # 1) Si NO viene project_id explícito pero sí #Proyecto en el texto,
        #    resolverlo o crearlo dentro de la carpeta actual.
        if project_id is None and quick_project_name:
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
            "INSERT INTO tasks(title, project_id, folder_id, due_date) VALUES(%s,%s,%s,%s)",
            (title, project_id, folder_id, due_date),
        )

        # 3) Etiquetas
        for t in tags:
            tag_id = get_or_create_tag(t)
            exec_sql(
                "INSERT IGNORE INTO task_tags(task_id, tag_id) VALUES(%s,%s)",
                (task_id, tag_id),
            )

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
        "SELECT id, title, notes, due_date, project_id, folder_id, recurrence_rule, completed_at "
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
        title = normalize_name(request.form.get("title", ""))
        notes = (request.form.get("notes") or "").strip() or None
        due_raw = (request.form.get("due_date") or "").strip()
        recurrence = (request.form.get("recurrence_rule") or "").strip() or None

        project_raw = (request.form.get("project_id") or "").strip()
        folder_raw = (request.form.get("folder_id") or "").strip()
        tags_csv_form = request.form.get("tags_csv") or ""

        if not title:
            flash("El título es obligatorio.", "error")
            return redirect(url_for("task_edit", task_id=task_id, next=next_url))

        # Fecha (YYYY-MM-DD del <input type="date">)
        due_date = None
        if due_raw:
            try:
                due_date = datetime.strptime(due_raw, "%Y-%m-%d").date()
            except ValueError:
                flash("Fecha inválida.", "error")
                return redirect(url_for("task_edit", task_id=task_id, next=next_url))

        # Proyecto / carpeta (exclusivos)
        project_id = None
        if project_raw:
            try:
                project_id = int(project_raw)
            except ValueError:
                project_id = None

        folder_id = None
        if folder_raw:
            try:
                folder_id = int(folder_raw)
            except ValueError:
                folder_id = None

        if project_id:
            folder_id = None
        elif folder_id:
            project_id = None

        try:
            exec_sql(
                "UPDATE tasks "
                "SET title=%s, notes=%s, due_date=%s, project_id=%s, folder_id=%s, recurrence_rule=%s "
                "WHERE id=%s",
                (title, notes, due_date, project_id, folder_id, recurrence, task_id),
            )

            # Tags: borrar y reinsertar
            exec_sql("DELETE FROM task_tags WHERE task_id=%s", (task_id,))
            for tname in parse_tags_csv(tags_csv_form):
                tid = get_or_create_tag(tname)
                exec_sql(
                    "INSERT IGNORE INTO task_tags(task_id, tag_id) VALUES(%s,%s)",
                    (task_id, tid),
                )

            commit()
            flash("Tarea actualizada.", "ok")
            # ✅ SIEMPRE vuelve al origen (normalizado con /gtdApp)
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
    task = q1("SELECT id FROM tasks WHERE id=%s", (task_id,))
    if not task:
        abort(404)

    try:
        # Borra primero dependencias conocidas (si no existen tablas, fallará y lo verás claro en el log)
        exec_sql("DELETE FROM task_tags WHERE task_id=%s", (task_id,))
        exec_sql("DELETE FROM subtasks WHERE task_id=%s", (task_id,))  # <- clave si hay subtareas

        # Por último la tarea
        exec_sql("DELETE FROM tasks WHERE id=%s", (task_id,))

        commit()
        flash("Tarea borrada.", "ok")
    except Exception as e:
        rollback()
        # Esto hace que el error real aparezca en error.log de Apache/mod_wsgi
        try:
            app.logger.exception("task_delete failed for task_id=%s", task_id)
        except Exception:
            pass
        flash(f"No se pudo borrar la tarea: {e}", "error")

    return redirect(request.form.get("next") or request.referrer or url_for("home"))



@app.route("/tasks/<int:task_id>/toggle", methods=["POST"])
def task_toggle(task_id: int):
    task = q1(
        "SELECT id, completed_at, due_date, recurrence_rule, project_id "
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
                next_due = next_due_date(task["due_date"], rule)
                exec_sql(
                    "UPDATE tasks SET last_completed_at=%s, due_date=%s, completed_at=NULL WHERE id=%s",
                    (now, next_due, task_id),
                )

            else:
                # CASO 2: tarea no recurrente
                next_tag = q1("SELECT id FROM tags WHERE name=%s", ("NextAction",))
                next_tag_id = int(next_tag["id"]) if next_tag else None

                has_nextaction = False
                if next_tag_id:
                    row = q1(
                        "SELECT 1 AS ok "
                        "FROM task_tags "
                        "WHERE task_id=%s AND tag_id=%s",
                        (task_id, next_tag_id),
                    )
                    has_nextaction = row is not None

                # Si tenía NextAction, quitársela antes de completar
                if has_nextaction:
                    exec_sql(
                        "DELETE FROM task_tags WHERE task_id=%s AND tag_id=%s",
                        (task_id, next_tag_id),
                    )

                # Marcar como hecha
                exec_sql("UPDATE tasks SET completed_at=%s WHERE id=%s", (now, task_id))

                # Si tenía NextAction y pertenece a un proyecto, promocionar la siguiente
                if has_nextaction and task.get("project_id"):
                    next_task = q1(
                        "SELECT id "
                        "FROM tasks "
                        "WHERE project_id=%s "
                        "AND completed_at IS NULL "
                        "AND id<>%s "
                        "ORDER BY (due_date IS NULL) ASC, due_date ASC, id ASC "
                        "LIMIT 1",
                        (task["project_id"], task_id),
                    )
                    if next_task:
                        exec_sql(
                            "INSERT IGNORE INTO task_tags(task_id, tag_id) VALUES(%s,%s)",
                            (next_task["id"], next_tag_id),
                        )

        commit()

    except Exception as e:
        rollback()
        flash(f"No se pudo actualizar la tarea: {e}", "error")

    return redirect(request.referrer or url_for("home"))


# ---------------- Routes: projects / folders CRUD ----------------

@app.route("/projects/create", methods=["POST"])
def project_create():
    name = normalize_name(request.form.get("name", ""))
    desc = (request.form.get("description") or "").strip() or None
    folder_id = request.form.get("folder_id") or None
    folder_id = int(folder_id) if folder_id else None

    if not name:
        flash("El nombre del proyecto es obligatorio.", "error")
        return redirect(url_for("projects"))

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

    return redirect(url_for("projects"))

@app.route("/projects/<int:project_id>/archive", methods=["POST"])
def project_archive(project_id: int):
    try:
        exec_sql("UPDATE projects SET archived=1, updated_at=NOW() WHERE id=%s", (project_id,))
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

    # Proyectos dentro de la carpeta (activos)
    projects = q(
        "SELECT id, name FROM projects WHERE folder_id=%s AND archived=0 ORDER BY name",
        (folder_id,)
    )

    # Tareas asignadas directamente a la carpeta (no a proyectos)
    tasks = q(
        "SELECT t.id, t.title, t.notes, t.due_date, t.completed_at, t.recurrence_rule "
        "FROM tasks t "
        "WHERE t.folder_id=%s AND t.project_id IS NULL "
        "ORDER BY (t.completed_at IS NOT NULL) ASC, (t.due_date IS NULL) ASC, t.due_date ASC, t.id DESC",
        (folder_id,)
    )
    tags_map = load_tags_map([t["id"] for t in tasks])
    
    task_ids = [t["id"] for t in tasks] if tasks else []

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

    return render_template("folder_detail.html", folder=folder, projects=projects, tasks=tasks, tags_map=tags_map, sub_map=sub_map, sub_counts=sub_counts,)

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
        f"WHERE {where_sql}",
        tuple(params),
    )
    total = int(total_row["c"]) if total_row else 0
    pages = max(1, (total + per_page - 1) // per_page)

    # Página de resultados
    sql = (
        "SELECT t.id, t.title, t.notes, t.due_date, t.completed_at, t.recurrence_rule, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "LEFT JOIN folders pf ON pf.id=p.folder_id "
        f"WHERE {where_sql} "
        "ORDER BY (t.completed_at IS NOT NULL) ASC, (t.due_date IS NULL) ASC, t.due_date ASC, t.id DESC "
        "LIMIT %s OFFSET %s"
    )

    rows = q(sql, tuple(params) + (per_page, offset))
    tags_map = load_tags_map([r["id"] for r in rows]) if rows else {}

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

def admin_required() -> bool:
    pwd = os.environ.get("GTD_ADMIN_PASSWORD", "")
    if not pwd:
        return False
    return session.get("is_admin") is True

@app.route("/admin", methods=["GET", "POST"])
def admin():
    env_pwd_set = bool(os.environ.get("GTD_ADMIN_PASSWORD", ""))

    if request.method == "POST":
        action = request.form.get("action", "")

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
                    "DELETE p FROM projects p "
                    "LEFT JOIN tasks t ON t.project_id = p.id "
                    "WHERE t.id IS NULL"
                )
                commit()
                flash("Proyectos vacíos borrados.", "ok")
            except Exception as e:
                rollback()
                flash(f"No se pudieron borrar los proyectos vacíos: {e}", "error")

            return redirect(url_for("admin"))
            
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

            try:
                # Borrar primero relaciones de etiquetas de las tareas afectadas
                exec_sql(
                    "DELETE tt FROM task_tags tt "
                    "JOIN tasks t ON t.id = tt.task_id "
                    "WHERE t.completed_at IS NOT NULL "
                    "AND t.completed_at < (NOW() - INTERVAL %s DAY)",
                    (days,),
                )

                # Si tienes subtareas, borrarlas también para esas tareas
                exec_sql(
                    "DELETE st FROM subtasks st "
                    "JOIN tasks t ON t.id = st.task_id "
                    "WHERE t.completed_at IS NOT NULL "
                    "AND t.completed_at < (NOW() - INTERVAL %s DAY)",
                    (days,),
                )

                # Finalmente borrar las tareas completadas antiguas
                exec_sql(
                    "DELETE FROM tasks "
                    "WHERE completed_at IS NOT NULL "
                    "AND completed_at < (NOW() - INTERVAL %s DAY)",
                    (days,),
                )

                commit()
                flash(f"Tareas realizadas con antigüedad superior a {days} días borradas.", "ok")
            except Exception as e:
                rollback()
                flash(f"No se pudieron borrar las tareas realizadas: {e}", "error")

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

            save_config(cfg)
            flash("Paginación guardada.", "ok")
            return redirect(url_for("admin"))

    cfg = load_config()
    
    return render_template(
        "admin.html",
        cfg=cfg,
        is_admin=admin_required(),
        env_pwd_set=env_pwd_set,
        backups=list_backups(),
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
            "WHERE completed_at IS NULL AND project_id IS NOT NULL "
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
        f"WHERE {where_sql}",
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
        f"WHERE {where_sql} "
        "ORDER BY (t.completed_at IS NOT NULL) ASC, (t.due_date IS NULL) ASC, t.due_date ASC, t.id DESC "
        "LIMIT %s OFFSET %s"
    )

    rows = q(sql, tuple(params) + (per_page, offset))

    tags_map = load_tags_map([r["id"] for r in rows]) if rows else {}

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
    def tag_exists(name: str) -> bool:
        row = q1("SELECT id FROM tags WHERE lower(name)=lower(%s)", (name,))
        return row is not None

    def folder_exists(name: str) -> bool:
        row = q1("SELECT id FROM folders WHERE lower(name)=lower(%s)", (name,))
        return row is not None

    # 1) Inbox
    inbox = q(
        "SELECT t.id, t.title, t.notes, t.due_date, t.completed_at, t.recurrence_rule, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "WHERE t.completed_at IS NULL "
        "AND t.project_id IS NULL "
        "AND t.folder_id IS NULL "
        "ORDER BY t.id DESC"
    ) or []

    # 2) NextActions
    nextaction_exists = tag_exists("NextAction")

    nextactions_open = q(
        "SELECT t.id, t.title, t.notes, t.due_date, t.completed_at, t.recurrence_rule, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "JOIN task_tags tt ON tt.task_id=t.id "
        "JOIN tags tg ON tg.id=tt.tag_id "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "WHERE t.completed_at IS NULL "
        "AND tg.name=%s "
        "AND (t.due_date IS NULL OR t.due_date >= CURDATE()) "
        "ORDER BY (t.due_date IS NULL) ASC, t.due_date ASC, t.id DESC",
        ("NextAction",)
    ) if nextaction_exists else []

    nextactions_overdue = q(
        "SELECT t.id, t.title, t.notes, t.due_date, t.completed_at, t.recurrence_rule, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "JOIN task_tags tt ON tt.task_id=t.id "
        "JOIN tags tg ON tg.id=tt.tag_id "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "WHERE t.completed_at IS NULL "
        "AND tg.name=%s "
        "AND t.due_date IS NOT NULL "
        "AND t.due_date < CURDATE() "
        "ORDER BY t.due_date ASC, t.id DESC",
        ("NextAction",)
    ) if nextaction_exists else []

    # 3) Agenda futura
    en_seguimiento_exists = tag_exists("EnSeguimiento")

    upcoming_7 = q(
        "SELECT t.id, t.title, t.notes, t.due_date, t.completed_at, t.recurrence_rule, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "WHERE t.completed_at IS NULL "
        "AND t.due_date IS NOT NULL "
        "AND t.due_date >= CURDATE() "
        "AND t.due_date <= DATE_ADD(CURDATE(), INTERVAL 7 DAY) "
        "ORDER BY t.due_date ASC, t.id DESC"
    ) or []

    en_seguimiento_tasks = q(
        "SELECT t.id, t.title, t.notes, t.due_date, t.completed_at, t.recurrence_rule, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "JOIN task_tags tt ON tt.task_id=t.id "
        "JOIN tags tg ON tg.id=tt.tag_id "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "WHERE t.completed_at IS NULL "
        "AND tg.name=%s "
        "ORDER BY (t.due_date IS NULL) ASC, t.due_date ASC, t.id DESC",
        ("EnSeguimiento",)
    ) if en_seguimiento_exists else []

    # 4) Agenda pasada
    agenda_exists = tag_exists("agenda")

    agenda_overdue = q(
        "SELECT t.id, t.title, t.due_date, t.notes, t.completed_at, t.recurrence_rule, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "JOIN task_tags tt ON tt.task_id=t.id "
        "JOIN tags tg ON tg.id=tt.tag_id "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "WHERE t.completed_at IS NULL "
        "AND tg.name=%s "
        "AND t.due_date IS NOT NULL "
        "AND t.due_date < CURDATE() "
        "ORDER BY t.due_date ASC, t.id DESC",
        ("agenda",)
    ) if agenda_exists else []

    # 5) EnEspera
    en_espera_exists = tag_exists("EnEspera")

    en_espera_tasks = q(
        "SELECT t.id, t.title, t.due_date, t.notes, t.completed_at, t.recurrence_rule, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "JOIN task_tags tt ON tt.task_id=t.id "
        "JOIN tags tg ON tg.id=tt.tag_id "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id=t.folder_id "
        "WHERE t.completed_at IS NULL "
        "AND tg.name=%s "
        "ORDER BY (t.due_date IS NULL) ASC, t.due_date ASC, t.id DESC",
        ("EnEspera",)
    ) if en_espera_exists else []

    # 6) Proyectos
    active_projects = q(
        "SELECT p.id, p.name, p.description, p.archived, f.name AS folder_name "
        "FROM projects p "
        "LEFT JOIN folders f ON f.id=p.folder_id "
        "WHERE p.archived=0 "
        "ORDER BY p.name"
    ) or []

    empty_projects = q(
        "SELECT p.id, p.name, p.description, p.archived, f.name AS folder_name "
        "FROM projects p "
        "LEFT JOIN folders f ON f.id=p.folder_id "
        "LEFT JOIN tasks t ON t.project_id = p.id AND t.completed_at IS NULL "
        "WHERE p.archived = 0 "
        "GROUP BY p.id, p.name, p.description, p.archived, f.name "
        "HAVING COUNT(t.id) = 0 "
        "ORDER BY p.name"
    ) or []

    # 7) ADTV / SomeTime
    adtv_folder_exists = folder_exists("ADTV")
    esta_semana_no_folder_exists = folder_exists("🔜 EstaSemanaNo")

    adtv_projects = q(
        "SELECT p.id, p.name, p.description, p.archived, f.name AS folder_name "
        "FROM projects p "
        "JOIN folders f ON f.id=p.folder_id "
        "WHERE p.archived=0 AND f.name=%s "
        "ORDER BY p.name",
        ("ADTV",)
    ) if adtv_folder_exists else []

    esta_semana_no_projects = q(
        "SELECT p.id, p.name, p.description, p.archived, f.name AS folder_name "
        "FROM projects p "
        "JOIN folders f ON f.id=p.folder_id "
        "WHERE p.archived=0 AND f.name=%s "
        "ORDER BY p.name",
        ("🔜 EstaSemanaNo",)
    ) if esta_semana_no_folder_exists else []

    # 8) Checklists
    checklists_folder_exists = folder_exists("✅ Checklists")

    checklist_tasks = q(
        "SELECT t.id, t.title, t.notes, t.due_date, t.completed_at, t.recurrence_rule, "
        "p.name AS project_name, p.id AS project_id, "
        "fd.name AS folder_name, fd.id AS folder_id "
        "FROM tasks t "
        "LEFT JOIN projects p ON p.id=t.project_id "
        "LEFT JOIN folders fd ON fd.id = COALESCE(t.folder_id, p.folder_id) "
        "WHERE t.completed_at IS NULL "
        "AND fd.name=%s "
        "ORDER BY t.id DESC",
        ("✅ Checklists",)
    ) if checklists_folder_exists else []

    # Tags map de todas las listas de tareas
    all_task_ids = []
    for group in (
        inbox,
        nextactions_open,
        nextactions_overdue,
        upcoming_7,
        en_seguimiento_tasks,
        agenda_overdue,
        en_espera_tasks,
        checklist_tasks,
    ):
        all_task_ids.extend([t["id"] for t in group])

    tags_map = load_tags_map(all_task_ids) if all_task_ids else {}

    return render_template(
        "review.html",
        inbox=inbox,
        nextaction_exists=nextaction_exists,
        nextactions_open=nextactions_open,
        nextactions_overdue=nextactions_overdue,
        en_seguimiento_exists=en_seguimiento_exists,
        upcoming_7=upcoming_7,
        en_seguimiento_tasks=en_seguimiento_tasks,
        agenda_exists=agenda_exists,
        agenda_overdue=agenda_overdue,
        en_espera_exists=en_espera_exists,
        en_espera_tasks=en_espera_tasks,
        active_projects=active_projects,
        empty_projects=empty_projects,
        adtv_folder_exists=adtv_folder_exists,
        adtv_projects=adtv_projects,
        esta_semana_no_folder_exists=esta_semana_no_folder_exists,
        esta_semana_no_projects=esta_semana_no_projects,
        checklists_folder_exists=checklists_folder_exists,
        checklist_tasks=checklist_tasks,
        tags_map=tags_map,
    )
    
    
@app.route("/next")
def next_actions():

    tag = q1("SELECT id FROM tags WHERE name='NextAction'")

    if not tag:
        rows = []
    else:
        rows = q(
            "SELECT t.id, t.title, t.notes, t.due_date, t.completed_at, "
            "p.name AS project_name, p.id AS project_id, "
            "fd.name AS folder_name, fd.id AS folder_id "
            "FROM tasks t "
            "JOIN task_tags tt ON tt.task_id=t.id "
            "LEFT JOIN projects p ON p.id=t.project_id "
            "LEFT JOIN folders fd ON fd.id = COALESCE(t.folder_id, p.folder_id) "
            "WHERE tt.tag_id=%s "
            "AND t.completed_at IS NULL "
            "ORDER BY (t.due_date IS NULL) ASC, t.due_date ASC, t.id DESC",
            (tag["id"],)
        )

    tags_map = load_tags_map([r["id"] for r in rows]) if rows else {}

    return render_template(
        "next.html",
        rows=rows,
        tags_map=tags_map,
    )
    
    
@app.route("/api/tags/search")
def api_tags_search():
    qtxt = (request.args.get("q") or "").strip().lower()

    if not qtxt:
        return jsonify({"items": []})

    rows = q(
        "SELECT id, name "
        "FROM tags "
        "WHERE LOWER(name) LIKE %s "
        "ORDER BY name "
        "LIMIT 8",
        (f"{qtxt}%",),
    )

    return jsonify({"items": rows})


@app.route("/gmail/import_to_inbox", methods=["POST"])
def gmail_import_to_inbox():
    """
    Importa correos de Gmail como tareas en Inbox.

    Requisitos:
    - instance/gmail_credentials.json
    - token OAuth generado en instance/gmail_token.json
    - tabla imported_emails
    """
    next_url = request.form.get("next") or request.referrer or url_for("home")

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

            task_id = exec_sql(
                "INSERT INTO tasks(title, notes, project_id, folder_id, due_date, recurrence_rule) "
                "VALUES(%s,%s,NULL,NULL,%s,NULL)",
                (
                    payload["title"],
                    payload["notes"],
                    payload["due_date"],
                ),
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
        flash(f"No se pudieron importar los correos: {e}", "error")

    return redirect(next_url)