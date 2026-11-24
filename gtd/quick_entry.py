"""Parser de entrada rápida (fechas, horas, prioridad, recurrencia, #proyecto, f:carpeta, @etiquetas)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import List, Optional, Tuple

from gtd.utils import _today_madrid, normalize_name, normalize_tag_key
from gtd.roles import role_tag_names


RECURRENCE_PATTERNS = {
    r"\bcada\s+d[ií]a\b": "FREQ=DAILY;INTERVAL=1",
    r"\bcada\s+semana\b": "FREQ=WEEKLY;INTERVAL=1",
    r"\bcada\s+mes\b": "FREQ=MONTHLY;INTERVAL=1",
    r"\bcada\s+año\b": "FREQ=YEARLY;INTERVAL=1",
    r"\bcada\s+laboral\b": "FREQ=WORKDAY;INTERVAL=1",
}


# URLs literales: nada dentro de ellas es un token (#fragmento, @usuario, 12:30, 2026/11…).
URL_RE = re.compile(r'(?:https?://|www\.)[^\s"<>]+', re.IGNORECASE)


def _mask_quoted_text(raw_text: str) -> str:
    """Copia del texto con lo que no debe interpretarse (comillas y URLs) cambiado por espacios.

    Mantiene las posiciones, así que una coincidencia en la copia sirve para recortar el original.
    """
    chars = list(raw_text or "")
    in_quotes = False
    for index, char in enumerate(chars):
        if char == '"':
            in_quotes = not in_quotes
            chars[index] = " "
        elif in_quotes:
            chars[index] = " "
    for match in URL_RE.finditer(raw_text or ""):
        chars[match.start():match.end()] = " " * (match.end() - match.start())
    return "".join(chars)


def _unquoted_matches(pattern, raw_text: str):
    text = raw_text or ""
    visible = _mask_quoted_text(text)
    return [match for match in pattern.finditer(text) if not visible[match.start()].isspace()]


def _first_unquoted_match(pattern, raw_text: str):
    matches = _unquoted_matches(pattern, raw_text)
    return matches[0] if matches else None


def _remove_unquoted_matches(raw_text: str, pattern) -> str:
    text = raw_text or ""
    for match in reversed(_unquoted_matches(pattern, text)):
        text = text[:match.start()] + " " + text[match.end():]
    return text


def _unquote_title(raw_text: str) -> str:
    return re.sub(r'"([^"]*)"', r"\1", raw_text or "")


def extract_recurrence_from_quick(raw_text: str, recurrence: Optional[str] = None) -> Tuple[Optional[str], str]:
    """Extract a supported `cada ...` recurrence token and remove it from the title."""
    cleaned = raw_text or ""
    if not recurrence:
        for pattern, rule in RECURRENCE_PATTERNS.items():
            if _first_unquoted_match(re.compile(pattern, flags=re.IGNORECASE), cleaned):
                recurrence = rule
                break
    for pattern in RECURRENCE_PATTERNS:
        cleaned = _remove_unquoted_matches(cleaned, re.compile(pattern, flags=re.IGNORECASE))
    return recurrence, re.sub(r"\s+", " ", cleaned).strip()


TAG_RE = re.compile(r"@([A-Za-z0-9_.\-áéíóúÁÉÍÓÚñÑ]+)")
# Etiqueta del Inbox: cualquier carácter salvo espacio, @ y #; la puntuación final
# (",", ".", ";", ":", "!", "?", ")") no forma parte del nombre pero sí del token.
QUICK_TAG_RE = re.compile(r'@([^\s@#]+?)[,.;:!?)\]]*(?=\s|$|[@#])')
# Grupo 1 = nombre entrecomillado (con espacios), grupo 2 = nombre sin comillas (sin espacios)
PROJ_RE = re.compile(r'#(?:"([^"]+)"|([A-Za-z0-9_\-áéíóúÁÉÍÓÚñÑ][A-Za-z0-9_\-áéíóúÁÉÍÓÚñÑ]*))')
FOLDER_RE = re.compile(r'(?<![a-zA-Z])f:(?:"([^"]+)"|([A-Za-z0-9_\-áéíóúÁÉÍÓÚñÑ][A-Za-z0-9_\-áéíóúÁÉÍÓÚñÑ]*))')


def is_periodic_tag_name(tag_name: str) -> bool:
    key = normalize_tag_key(tag_name)
    return key in {normalize_tag_key(n) for n in role_tag_names("periodic")}
    
    
DATE_TOKEN_RE = re.compile(
    r'(?<!\S)\*('
    r'\d{1,2}(?:-|/)\d{1,2}(?:(?:-|/)\d{4})?'
    r'|hoy'
    r'|mañana'
    r'|lunes|martes|miércoles|miercoles|jueves|viernes|sábado|sabado|domingo'
    r'|\+\d+'
    r')\b',
    re.IGNORECASE
)

DATE_BARE_RE = re.compile(
    r'(?<![#@\w])('
    r'\d{1,2}(?:-|/)\d{1,2}(?:(?:-|/)\d{4})?'
    r')\b'
)

TIME_TOKEN_RE = re.compile(r'(?<!\S)(?:h:)?(\d{1,2}:\d{2})\b', re.IGNORECASE)
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

    m = PRIORITY_TOKEN_RE.search(_mask_quoted_text(s))
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

    weekdays = {
        "lunes": 0,
        "martes": 1,
        "miércoles": 2,
        "miercoles": 2,
        "jueves": 3,
        "viernes": 4,
        "sábado": 5,
        "sabado": 5,
        "domingo": 6,
    }
    if token in weekdays:
        days_ahead = (weekdays[token] - today.weekday()) % 7 or 7
        return today + timedelta(days=days_ahead)

    if token.startswith("+") and token[1:].isdigit():
        return today + timedelta(days=int(token[1:]))

    m = re.fullmatch(r'(\d{1,2})([-/])(\d{1,2})(?:\2(\d{4}))?', token)
    if m:
        day = int(m.group(1))
        month = int(m.group(3))
        year = int(m.group(4)) if m.group(4) else today.year
        return datetime.strptime(f"{day:02d}-{month:02d}-{year}", "%d-%m-%Y").date()

    raise ValueError(f"Fecha inválida: {token}")


def extract_due_date_from_quick(raw_text: str) -> Tuple[Optional[date], str]:
    """
    Devuelve (due_date, cleaned_text)

    Soporta:
    - *15-03 o *15/03
    - *15-03-2026 o *15/03/2026
    - *hoy
    - *mañana
    - *lunes (también martes, miércoles, jueves, viernes, sábado y domingo)
    - *+3
    - 15-03 o 15/03
    - 15-03-2026 o 15/03/2026

    Prioridad:
    1) fecha con *
    2) fecha 'bare' sin prefijo
    """
    s = (raw_text or "").strip()
    if not s:
        return None, s

    # 1) Buscar primero fecha con prefijo *
    visible = _mask_quoted_text(s)
    m = DATE_TOKEN_RE.search(visible)
    if m:
        token = m.group(1)
        due_date = parse_due_token(token)
        cleaned = (s[:m.start()] + " " + s[m.end():]).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        return due_date, cleaned

    # 2) Buscar palabras clave de fecha sin prefijo
    m = re.search(
        r'(?<!\w)(hoy|mañana|lunes|martes|miércoles|miercoles|jueves|viernes|sábado|sabado|domingo)(?!\w)',
        visible,
        re.IGNORECASE,
    )
    if m:
        token = m.group(1).lower()
        due_date = parse_due_token(token)
        cleaned = (s[:m.start()] + " " + s[m.end():]).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        return due_date, cleaned

    # 3) Si no hay *, buscar fecha "bare" (sin prefijo)
    m = DATE_BARE_RE.search(visible)
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

    Soporta HH:MM y h:HH:MM
    Ejemplos válidos: 23:30, 8:05, h:23:30, h:8:05
    """
    s = (raw_text or '').strip()
    if not s:
        return None, s

    m = TIME_TOKEN_RE.search(_mask_quoted_text(s))
    if not m:
        return None, s

    due_time = parse_due_time_token(m.group(1))
    cleaned = (s[:m.start()] + " " + s[m.end():]).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return due_time, cleaned


def parse_task_quick_entry(raw_title: str) -> Tuple[str, List[str], Optional[str], Optional[str]]:
    raw = raw_title or ""
    tags = [match.group(1) for match in _unquoted_matches(TAG_RE, raw)]
    m = _first_unquoted_match(PROJ_RE, raw)
    project_name = (m.group(1) or m.group(2) or '').strip() if m else None
    m_folder = _first_unquoted_match(FOLDER_RE, raw)
    folder_name = (m_folder.group(1) or m_folder.group(2) or '').strip() if m_folder else None

    title = _remove_unquoted_matches(raw, TAG_RE)
    title = _remove_unquoted_matches(title, PROJ_RE)
    title = _remove_unquoted_matches(title, FOLDER_RE)
    title = _unquote_title(title)
    title = re.sub(r"\s+", " ", title).strip()

    tags = [normalize_name(t) for t in tags if normalize_name(t)]
    if project_name:
        project_name = normalize_name(project_name)
    if folder_name:
        folder_name = normalize_name(folder_name)
    return title, tags, project_name, folder_name
    
  
    


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


@dataclass
class QuickEntry:
    """Resultado de interpretar una línea de entrada rápida."""
    title: str
    tags: List[str] = field(default_factory=list)
    project: Optional[str] = None
    folder: Optional[str] = None
    due_date: Optional[date] = None
    due_time: Optional[time] = None
    priority: Optional[int] = None
    recurrence: Optional[str] = None


_DATE_LIKE_PROJECT_RE = re.compile(r"\d{2}-\d{2}-\d{4}")


def parse_quick_entry(raw: str, *, parse_date: bool = True, parse_time: bool = True,
                      recurrence: Optional[str] = None) -> QuickEntry:
    """Interpreta ``Tarea @etiqueta #proyecto f:carpeta *fecha HH:MM ^prioridad cada …``.

    Es la sintaxis del Inbox (``/tasks/create``). Orden de extracción:
    etiquetas (sobre el texto original) → prioridad → fecha → hora → recurrencia →
    proyecto → carpeta → título. El texto entre comillas no se interpreta.

    ``parse_date``/``parse_time`` a False cuando el formulario ya trae fecha/hora
    (el token queda entonces en el título). ``recurrence`` es la regla del formulario,
    que prevalece sobre ``cada …``. Lanza ``ValueError`` si una fecha u hora es inválida.
    """
    work = raw or ""
    tags = [match.group(1) for match in _unquoted_matches(QUICK_TAG_RE, work)]

    priority, work = extract_priority_from_quick(work)
    due_date = due_time = None
    if parse_date:
        due_date, work = extract_due_date_from_quick(work)
    if parse_time:
        due_time, work = extract_due_time_from_quick(work)
    recurrence, work = extract_recurrence_from_quick(work, recurrence)

    project = None
    m_proj = _first_unquoted_match(PROJ_RE, work)
    if m_proj:
        candidate = (m_proj.group(1) or m_proj.group(2) or "").strip()
        if not _DATE_LIKE_PROJECT_RE.fullmatch(candidate):
            project = candidate

    folder = None
    m_folder = _first_unquoted_match(FOLDER_RE, work)
    if m_folder:
        folder = (m_folder.group(1) or m_folder.group(2) or "").strip()

    title = _remove_unquoted_matches(work, QUICK_TAG_RE)
    title = _remove_unquoted_matches(title, PROJ_RE)
    title = _remove_unquoted_matches(title, FOLDER_RE)
    title = _unquote_title(title)
    title = re.sub(r"\s+", " ", title).strip(" -_,.;:")

    return QuickEntry(title=title, tags=tags, project=project, folder=folder, due_date=due_date,
                      due_time=due_time, priority=priority, recurrence=recurrence)
