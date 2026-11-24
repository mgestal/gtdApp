"""Migraciones de esquema versionadas.

Cada cambio de esquema es un fichero ``migrations/NNNN_descripcion.py`` con una
función ``upgrade(ctx)``; ``ctx`` ofrece ``exec_sql``, ``q`` y ``q1``. Las versiones
aplicadas se registran en la tabla ``schema_migrations``.

- Al primer uso de la BD en cada proceso (``ensure_schema_updates``, llamado desde
  el ``before_request``) se aplican las pendientes en orden.
- Un ``GET_LOCK`` de MySQL evita que dos procesos migren a la vez.
- MySQL confirma el DDL implícitamente: si una migración falla a medias no se puede
  deshacer, así que escríbelas idempotentes (``IF NOT EXISTS``, comprobaciones previas).
- CLI: ``.venv/bin/python migrate.py status | up | new "descripción"``.
"""
from __future__ import annotations

import importlib.util
import re
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Dict, Iterable, List, Optional

from gtd.config import BASE_DIR
from gtd.db import commit, exec_sql, q, q1, rollback

MIGRATIONS_DIR = BASE_DIR / "migrations"
MIGRATION_FILE_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.py$")
LOCK_NAME = "gtd_schema_migrations"
LOCK_TIMEOUT_SECONDS = 60

MIGRATIONS_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    "version CHAR(4) NOT NULL PRIMARY KEY, "
    "name VARCHAR(255) NOT NULL, "
    "applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

_schema_bootstrapped = False


@dataclass(frozen=True)
class Migration:
    version: str   # "0001"
    name: str      # "baseline"
    path: Path

    @property
    def label(self) -> str:
        return f"{self.version}_{self.name}"

    def load(self) -> ModuleType:
        spec = importlib.util.spec_from_file_location(f"gtd_migration_{self.label}", self.path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not callable(getattr(module, "upgrade", None)):
            raise RuntimeError(f"La migración {self.label} no define upgrade(ctx)")
        return module


@dataclass
class MigrationContext:
    """Lo que recibe ``upgrade(ctx)``."""
    exec_sql: Callable[..., Any]
    q: Callable[..., List[Dict[str, Any]]]
    q1: Callable[..., Optional[Dict[str, Any]]]


def discover_migrations(directory: Optional[Path] = None) -> List[Migration]:
    """Migraciones del directorio, ordenadas por versión. Falla si hay versiones repetidas."""
    directory = directory or MIGRATIONS_DIR
    found: Dict[str, Migration] = {}
    for path in sorted(directory.glob("*.py")):
        match = MIGRATION_FILE_RE.match(path.name)
        if not match:
            continue
        version, name = match.groups()
        if version in found:
            raise RuntimeError(f"Versión de migración duplicada: {found[version].path.name} y {path.name}")
        found[version] = Migration(version, name, path)
    return [found[v] for v in sorted(found)]


def pending_migrations(available: Iterable[Migration], applied: Iterable[str]) -> List[Migration]:
    applied_set = set(applied)
    return [m for m in available if m.version not in applied_set]


def applied_versions(create_table: bool = True) -> List[str]:
    """Versiones aplicadas. Con ``create_table=False`` no modifica la BD (tabla ausente = ninguna)."""
    if create_table:
        exec_sql(MIGRATIONS_TABLE_SQL)
    elif not q1("SELECT 1 AS ok FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='schema_migrations'"):
        return []
    return [row["version"] for row in q("SELECT version FROM schema_migrations ORDER BY version")]


def apply_migrations(log: Callable[[str], None] = lambda _msg: None) -> List[str]:
    """Aplica las migraciones pendientes (con bloqueo entre procesos). Devuelve las aplicadas."""
    lock = q1("SELECT GET_LOCK(%s, %s) AS ok", (LOCK_NAME, LOCK_TIMEOUT_SECONDS))
    if not lock or lock.get("ok") != 1:
        raise RuntimeError("No se pudo obtener el bloqueo de migraciones (¿otra migración en curso?)")
    done: List[str] = []
    try:
        pending = pending_migrations(discover_migrations(), applied_versions())
        ctx = MigrationContext(exec_sql=exec_sql, q=q, q1=q1)
        for migration in pending:
            log(f"Aplicando {migration.label}...")
            try:
                migration.load().upgrade(ctx)
                exec_sql(
                    "INSERT INTO schema_migrations(version, name) VALUES(%s, %s)",
                    (migration.version, migration.name),
                )
                commit()
            except Exception:
                rollback()
                raise
            done.append(migration.label)
    finally:
        q1("SELECT RELEASE_LOCK(%s) AS ok", (LOCK_NAME,))
    return done


def ensure_schema_updates() -> None:
    """Aplica migraciones pendientes una sola vez por proceso."""
    global _schema_bootstrapped
    if _schema_bootstrapped:
        return
    apply_migrations()
    _schema_bootstrapped = True


def next_migration_path(description: str, directory: Optional[Path] = None) -> Path:
    """Ruta para una migración nueva: siguiente número + descripción en snake_case ASCII."""
    directory = directory or MIGRATIONS_DIR
    import unicodedata
    slug = unicodedata.normalize("NFKD", description).encode("ascii", "ignore").decode().lower()
    slug = re.sub(r"[^a-z0-9]+", "_", slug).strip("_")
    if not slug:
        raise ValueError("La descripción debe contener letras o números")
    existing = discover_migrations(directory)
    number = int(existing[-1].version) + 1 if existing else 1
    return directory / f"{number:04d}_{slug}.py"
