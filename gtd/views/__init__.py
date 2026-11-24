"""Rutas (vistas) de GTD App, un módulo por área.

Importar este paquete registra todas las rutas en ``gtd.core.app``.
Los nombres de endpoint son los de siempre (sin prefijo de Blueprint).
"""
from gtd.views import (  # noqa: F401
    inbox,
    lists,
    kanban,
    projects,
    tags,
    notifications,
    periodic,
    dashboard,
    manual,
    tasks,
    folders,
    filters,
    search,
    review,
    trash_archive,
    calendar_admin,
    admin,
    extension,
    google_import,
)
