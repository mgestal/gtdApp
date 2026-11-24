"""Búsqueda/creación de etiquetas, tipos, proyectos y carpetas por nombre."""
from __future__ import annotations

from typing import Optional

from gtd.utils import normalize_name
from gtd.db import exec_sql, q1


def get_or_create_tag(tag_name: str) -> int:
    tag_name = normalize_name(tag_name)
    row = q1("SELECT id FROM tags WHERE name=%s", (tag_name,))
    if row:
        return int(row["id"])
    return exec_sql("INSERT INTO tags(name) VALUES(%s)", (tag_name,))


def get_or_create_tag_type(type_name: str) -> int:
    type_name = normalize_name(type_name)
    row = q1("SELECT id FROM TagsType WHERE name=%s", (type_name,))
    if row:
        return int(row["id"])
    return exec_sql("INSERT INTO TagsType(name) VALUES(%s)", (type_name,))

def find_project_by_name_active(project_name: str) -> Optional[int]:
    project_name = normalize_name(project_name)
    row = q1("SELECT id FROM projects WHERE name=%s AND archived=0", (project_name,))
    return int(row["id"]) if row else None


def find_folder_by_name(folder_name: str) -> Optional[int]:
    folder_name = normalize_name(folder_name)
    row = q1("SELECT id FROM folders WHERE name=%s", (folder_name,))
    return int(row["id"]) if row else None
