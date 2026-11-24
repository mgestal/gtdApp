"""Roles GTD: mapeo "rol → etiqueta(s) / carpeta(s)".

La app usa ciertas etiquetas y carpetas con significado especial (NextAction,
EnEspera, Sometime, ...). En lugar de tener los nombres escritos en el código,
cada rol se resuelve aquí a partir de ``config.json`` -> ``gtd_roles``:

    "gtd_roles": {
      "tags":    {"next_action": "NextAction", "periodic": ["Periodica", "Periodicas"]},
      "folders": {"someday": "Sometime", "agenda": ["Agenda 🗃️", "🗃️ Agenda"]}
    }

Cada valor puede ser una cadena, una lista o una cadena separada por comas.
El primer nombre es el canónico (el que se crea si falta); el resto son alias
que también se reconocen. Los roles no configurados usan los valores por defecto.

Este módulo no depende de Flask ni de la BD para poder usarse desde scripts
(bot de Telegram, CLI) y tests.
"""
from __future__ import annotations

import unicodedata
from typing import Any, Dict, List

# (rol, nombres por defecto, etiqueta para la UI, descripción)
TAG_ROLES: List[tuple] = [
    ("next_action", ["NextAction"], "Acción siguiente",
     "Vista Next, contador de la barra lateral, promoción automática en proyectos y revisión semanal."),
    ("following", ["EnSeguimiento"], "En seguimiento",
     "Bloque de seguimiento a 15 días en la revisión semanal."),
    ("agenda", ["agenda"], "Agenda",
     "Agenda futura/pasada en la revisión y etiqueta añadida a los eventos importados de Calendar."),
    ("waiting", ["EnEspera"], "En espera / delegado",
     "Bloque EnEspera de la revisión y acción «Delegar» al procesar el Inbox."),
    ("periodic", ["Periodica", "Periódica", "Periodicas", "Periódicas"], "Periódicas",
     "Tareas periódicas: historial y exclusión de los archivados/borrados automáticos."),
]

FOLDER_ROLES: List[tuple] = [
    ("agenda", ["Agenda 🗃️", "🗃️ Agenda", "Agenda"], "Agenda",
     "Subcard Agenda del Kanban; excluida de los proyectos activos."),
    ("routines", ["♲ Rutinas", "Rutinas"], "Rutinas",
     "Bloque de rutinas en la revisión; excluida de los proyectos activos."),
    ("waiting", ["EnEspera"], "En espera",
     "Proyectos en espera en la revisión."),
    ("following", ["Seguimiento"], "Seguimiento",
     "Proyectos en seguimiento; excluida de los proyectos activos."),
    ("someday", ["Sometime"], "Algún día / Sometime",
     "Revisión SomeTime y acción «Algún día» al procesar el Inbox."),
    ("adtv", ["ADTV"], "ADTV",
     "Proyectos ADTV en la revisión; excluida de los proyectos activos."),
    ("not_this_week", ["🔜 EstaSemanaNo"], "Esta semana no",
     "Proyectos aplazados en la revisión."),
    ("checklists", ["✅ Checklists"], "Checklists",
     "Bloque de checklists en la revisión."),
]

DEFAULT_TAG_ROLES: Dict[str, List[str]] = {r[0]: list(r[1]) for r in TAG_ROLES}
DEFAULT_FOLDER_ROLES: Dict[str, List[str]] = {r[0]: list(r[1]) for r in FOLDER_ROLES}

# Roles de carpeta cuyos árboles no cuentan como "proyectos activos" (dashboard/búsqueda).
NON_ACTIVE_PROJECT_FOLDER_ROLES = ("routines", "agenda", "someday", "adtv", "following")


def normalize_key(name: str) -> str:
    """Minúsculas, sin '@' inicial y sin tildes (para comparar nombres)."""
    raw = (name or "").strip().lower().lstrip("@")
    normalized = unicodedata.normalize("NFD", raw)
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def parse_names(value: Any) -> List[str]:
    """Convierte str / lista / 'a, b' en una lista de nombres sin vacíos ni duplicados."""
    if value is None:
        return []
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple)):
        items = []
        for v in value:
            items.extend(str(v).split(",") if v is not None else [])
    else:
        items = [str(value)]
    out: List[str] = []
    seen = set()
    for item in items:
        name = item.strip().lstrip("@").strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out


def resolve_roles(cfg: Dict[str, Any] | None) -> Dict[str, Dict[str, List[str]]]:
    """Devuelve {"tags": {rol: [nombres]}, "folders": {rol: [nombres]}} aplicando la config."""
    section = (cfg or {}).get("gtd_roles") or {}
    if not isinstance(section, dict):
        section = {}
    result: Dict[str, Dict[str, List[str]]] = {}
    for kind, defaults in (("tags", DEFAULT_TAG_ROLES), ("folders", DEFAULT_FOLDER_ROLES)):
        overrides = section.get(kind) or {}
        if not isinstance(overrides, dict):
            overrides = {}
        resolved = {}
        for role, default_names in defaults.items():
            names = parse_names(overrides.get(role))
            resolved[role] = names or list(default_names)
        result[kind] = resolved
    return result


def sql_name_variants(names: List[str]) -> List[str]:
    """Variantes en minúsculas (con y sin tildes) para comparar con LOWER(name) IN (...)."""
    out: List[str] = []
    for name in names:
        for variant in (name.strip().lower(), normalize_key(name)):
            if variant and variant not in out:
                out.append(variant)
    return out
