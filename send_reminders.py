#!/usr/bin/env python3
"""Send due Telegram reminders once; intended to run every 15 minutes from cron."""

import argparse
import sys
import time
from zoneinfo import ZoneInfo

from flask import g

from app import app, db, ensure_schema_updates, load_config
from reminders import load_reminder_token, notification_hours, run_worker
from telegram_gtdAppbot import load_chat_id


def main() -> int:
    with app.app_context():
        try:
            ensure_schema_updates()
            cfg = load_config().get("app", {})
            start, end = notification_hours(cfg)
            counts = run_worker(
                db(), ZoneInfo(cfg.get("timezone", "Europe/Madrid")),
                load_reminder_token, load_chat_id, cfg.get("public_url", ""), start, end,
            )
            print(f"Recordatorios: {counts}")
            return 1 if counts["failed"] else 0
        except Exception as exc:
            print(f"No se pudo ejecutar el envío de recordatorios ({type(exc).__name__}): {exc}",
                  file=sys.stderr)
            return 1
        finally:
            conn = getattr(g, "db_conn", None)
            if conn:
                conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", action="store_true", help="Ejecutar cada 15 minutos, para Docker")
    args = parser.parse_args()
    if args.loop:
        while True:
            main()
            time.sleep(15 * 60)
    else:
        raise SystemExit(main())
