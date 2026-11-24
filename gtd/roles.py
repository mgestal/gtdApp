"""Roles GTD: etiquetas/carpetas especiales configurables (ver gtd_roles.py)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from flask import g

import gtd_roles as _gtd_roles_mod

from gtd.config import load_config
from gtd.utils import normalize_tag_key
from gtd.db import exec_sql, q, q1
from gtd.lookups import get_or_create_tag


def ensure_review_defaults() -> None:
    """Inserta etiquetas/carpetas necesarias para que review funcione con todos los bloques."""
    required_tags = [role_tag(r) for r in ("next_action", "following", "agenda", "waiting")]
    required_folders = [role_folder(r) for r in ("checklists", "adtv", "not_this_week", "agenda")]

    # Si existe la carpeta de Agenda con un alias antiguo (p.ej. "🗃️ Agenda"), renombrarla al canónico.
    agenda_names = role_folder_names("agenda")
    agenda_canonical = agenda_names[0]
    renamed_agenda = q1(
        "SELECT id FROM folders WHERE name=%s AND parent_id IS NULL",
        (agenda_canonical,),
    )
    if not renamed_agenda:
        for legacy_name in agenda_names[1:]:
            legacy_agenda = q1(
                "SELECT id FROM folders WHERE name=%s AND parent_id IS NULL",
                (legacy_name,),
            )
            if legacy_agenda:
                exec_sql("UPDATE folders SET name=%s WHERE id=%s", (agenda_canonical, legacy_agenda["id"]))
                break

    for t in required_tags:
        if not tag_exists(t):
            exec_sql("INSERT IGNORE INTO tags(name) VALUES(%s)", (t,))

    for f in required_folders:
        if not folder_exists(f):
            exec_sql("INSERT IGNORE INTO folders(name, parent_id) VALUES(%s,%s)", (f, None))


# --- Funciones auxiliares para review y otros ---
def tag_exists(name: str) -> bool:
    row = q1("SELECT id FROM tags WHERE lower(name)=lower(%s)", (name,))
    return row is not None

def folder_exists(name: str) -> bool:
    row = q1("SELECT id FROM folders WHERE lower(name)=lower(%s)", (name,))
    return row is not None

def get_folder_tree_ids(parent_id, include_self=True):
    """Obtiene recursivamente los IDs de una carpeta y sus descendientes"""
    result = set()
    if include_self:
        result.add(parent_id)
    children = q("SELECT id FROM folders WHERE parent_id=%s", (parent_id,))
    for child in children:
        result.add(child['id'])
        result.update(get_folder_tree_ids(child['id'], include_self=False))
    return result


# --- Roles GTD (etiquetas/carpetas especiales configurables, ver gtd_roles.py) ---
def gtd_roles() -> Dict[str, Dict[str, List[str]]]:
    """Roles resueltos desde config.json (cacheados por petición)."""
    try:
        cached = getattr(g, "_gtd_roles", None)
        if cached is None:
            cached = _gtd_roles_mod.resolve_roles(load_config())
            g._gtd_roles = cached
        return cached
    except RuntimeError:  # fuera de contexto de petición
        return _gtd_roles_mod.resolve_roles(load_config())


def role_tag_names(role: str) -> List[str]:
    return gtd_roles()["tags"][role]


def role_tag(role: str) -> str:
    """Nombre canónico de la etiqueta asociada al rol (p.ej. 'NextAction')."""
    return role_tag_names(role)[0]


def role_tag_sql_names(role: str) -> List[str]:
    """Nombres en minúsculas (con y sin tildes) para `LOWER(tg.name) IN (...)`."""
    return _gtd_roles_mod.sql_name_variants(role_tag_names(role))


def role_folder_names(role: str) -> List[str]:
    return gtd_roles()["folders"][role]


def role_folder(role: str) -> str:
    """Nombre canónico de la carpeta asociada al rol (p.ej. 'Sometime')."""
    return role_folder_names(role)[0]


def role_folder_rows(role: str) -> List[Dict[str, Any]]:
    """Carpetas cuyo nombre coincide (sin distinguir mayúsculas) con el rol, en el orden configurado."""
    names = role_folder_names(role)
    rows = q(
        "SELECT id, name, parent_id FROM folders WHERE LOWER(name) IN ("
        + ",".join(["LOWER(%s)"] * len(names)) + ")",
        tuple(names),
    ) or []
    rank = {n.lower(): i for i, n in enumerate(names)}
    return sorted(rows, key=lambda r: (rank.get((r.get("name") or "").lower(), len(names)), r["id"]))


def role_folder_row(role: str) -> Optional[Dict[str, Any]]:
    rows = role_folder_rows(role)
    return rows[0] if rows else None


def role_folder_exists(role: str) -> bool:
    return role_folder_row(role) is not None


def role_folder_tree_ids(role: str) -> set:
    """IDs de todas las carpetas del rol y sus subcarpetas."""
    ids: set = set()
    for row in role_folder_rows(role):
        ids.update(get_folder_tree_ids(int(row["id"])))
    return ids


def non_active_project_folder_ids() -> set:
    """Carpetas (con subcarpetas) cuyos proyectos no cuentan como activos en dashboard/búsqueda."""
    names = {
        normalize_tag_key(n)
        for role in _gtd_roles_mod.NON_ACTIVE_PROJECT_FOLDER_ROLES
        for n in role_folder_names(role)
    }
    folder_rows = q("SELECT id, parent_id, name FROM folders") or []
    children_by_parent: Dict[Optional[int], List[int]] = {}
    excluded: set = set()
    for fr in folder_rows:
        children_by_parent.setdefault(fr.get("parent_id"), []).append(int(fr["id"]))
        if normalize_tag_key(fr.get("name") or "") in names:
            excluded.add(int(fr["id"]))
    stack = list(excluded)
    while stack:
        fid = stack.pop()
        for ch_id in children_by_parent.get(fid, []):
            if ch_id not in excluded:
                excluded.add(ch_id)
                stack.append(ch_id)
    return excluded


def task_has_role_tag(task_id: int, role: str) -> bool:
    return q1(
        "SELECT 1 AS ok FROM task_tags tt JOIN tags tg ON tg.id=tt.tag_id "
        "WHERE tt.task_id=%s AND LOWER(tg.name)=LOWER(%s) LIMIT 1",
        (task_id, role_tag(role)),
    ) is not None


def role_tag_id(role: str) -> int:
    """ID de la etiqueta del rol (la crea si no existe)."""
    row = q1("SELECT id FROM tags WHERE LOWER(name)=LOWER(%s) ORDER BY id ASC LIMIT 1", (role_tag(role),))
    return int(row["id"]) if row else get_or_create_tag(role_tag(role))
