from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pymysql


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "instance" / "config.json"


def load_db_cfg() -> dict:
    if not CONFIG_PATH.exists():
        raise RuntimeError(f"No existe el fichero de configuracion: {CONFIG_PATH}")

    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    db = data.get("db", {})

    required = ["host", "port", "user", "password", "database"]
    missing = [k for k in required if k not in db]
    if missing:
        raise RuntimeError(f"Faltan claves de DB en config.json: {', '.join(missing)}")

    return {
        "host": db["host"],
        "port": int(db["port"]),
        "user": db["user"],
        "password": db["password"],
        "database": db["database"],
        "charset": db.get("charset", "utf8mb4"),
    }


def main() -> None:
    db_cfg = load_db_cfg()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_table = f"tasks_priority_backup_{ts}"

    conn = pymysql.connect(
        host=db_cfg["host"],
        port=db_cfg["port"],
        user=db_cfg["user"],
        password=db_cfg["password"],
        database=db_cfg["database"],
        charset=db_cfg["charset"],
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM tasks WHERE priority IS NOT NULL")
            total_before = int(cur.fetchone()["c"])

            if total_before == 0:
                print("No hay tareas con prioridad establecida. Nada que actualizar.")
                conn.rollback()
                return

            print(f"Tareas con prioridad no NULL antes: {total_before}")
            print(f"Creando backup en tabla: {backup_table}")

            cur.execute(
                f"CREATE TABLE {backup_table} AS "
                "SELECT id, priority, created_at, completed_at FROM tasks WHERE priority IS NOT NULL"
            )

            cur.execute("UPDATE tasks SET priority=NULL WHERE priority IS NOT NULL")
            updated = cur.rowcount

            cur.execute("SELECT COUNT(*) AS c FROM tasks WHERE priority IS NOT NULL")
            total_after = int(cur.fetchone()["c"])

            conn.commit()

            print(f"Filas actualizadas: {updated}")
            print(f"Tareas con prioridad no NULL despues: {total_after}")
            print("OK. Prioridades puestas en NULL.")

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
