#!/usr/bin/env python3
"""Migraciones de esquema de GTD App.

Uso (desde /var/www/gtdApp):
    .venv/bin/python migrate.py status               # aplicadas y pendientes
    .venv/bin/python migrate.py up                   # aplica las pendientes
    .venv/bin/python migrate.py new "añadir campo x" # crea migrations/NNNN_anadir_campo_x.py

La app aplica las pendientes sola en la primera petición de cada proceso; ``up``
sirve para hacerlo antes de recargar (``touch wsgi.py``) y ver errores en consola.
"""
from __future__ import annotations

import argparse
import sys

from gtd.core import app
from gtd.db import db
from gtd.schema import (
    apply_migrations,
    applied_versions,
    discover_migrations,
    next_migration_path,
    pending_migrations,
)

TEMPLATE = '''"""{description}"""


def upgrade(ctx):
    # ctx.exec_sql(sql, params), ctx.q(sql, params) -> filas, ctx.q1(sql, params) -> fila | None
    # Hazla idempotente: ADD COLUMN IF NOT EXISTS, CREATE TABLE IF NOT EXISTS...
    ctx.exec_sql("...")
'''


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="muestra migraciones aplicadas y pendientes")
    sub.add_parser("up", help="aplica las migraciones pendientes")
    new = sub.add_parser("new", help="crea una migración vacía")
    new.add_argument("description")
    args = parser.parse_args(argv)

    if args.command == "new":
        path = next_migration_path(args.description)
        path.write_text(TEMPLATE.format(description=args.description), encoding="utf-8")
        print(f"Creada {path.relative_to(path.parent.parent)}")
        return 0

    with app.app_context():
        try:
            if args.command == "status":
                applied = set(applied_versions(create_table=False))
                for m in discover_migrations():
                    print(f"[{'x' if m.version in applied else ' '}] {m.label}")
                pending = pending_migrations(discover_migrations(), applied)
                print(f"{len(pending)} pendiente(s)")
            else:
                done = apply_migrations(log=print)
                print(f"{len(done)} migración(es) aplicada(s)" if done else "Nada que aplicar")
        finally:
            db().close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
