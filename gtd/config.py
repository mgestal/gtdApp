"""Configuración (instance/config.json) y lectura de valores."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from flask import g


# Raíz del proyecto (este fichero vive en gtd/).
BASE_DIR = Path(__file__).resolve().parent.parent
INSTANCE_DIR = BASE_DIR / "instance"
CONFIG_PATH = INSTANCE_DIR / "config.json"

DEFAULT_KANBAN_SUBCARDS = [
    {"key": "agenda", "label": "Agenda", "folder_name": "Agenda 🗃️", "tag_key": "agenda"},
    {"key": "rutinas", "label": "♲ Rutinas", "folder_name": "♲ Rutinas", "tag_key": ""},
    {"key": "en-espera", "label": "EnEspera", "folder_name": "EnEspera", "tag_key": ""},
    {"key": "seguimiento", "label": "Seguimiento", "folder_name": "Seguimiento", "tag_key": ""},
    {"key": "sometime", "label": "Sometime", "folder_name": "Sometime", "tag_key": ""},
]

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
    "kanban": {
        "columns": [
            {"key": "next", "label": "Próximo", "tag_name": "NextAction"},
            {"key": "doing", "label": "En proceso", "tag_name": "enCurso"},
            {"key": "waiting", "label": "Esperando", "tag_name": "EnEspera"},
        ],
        "subcards": DEFAULT_KANBAN_SUBCARDS,
    },
}


BACKUP_DIR = BASE_DIR / "backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)


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
