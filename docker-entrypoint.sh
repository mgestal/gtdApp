#!/bin/sh
set -eu

mkdir -p instance backups

if [ ! -f instance/config.json ]; then
  if [ -f instance/config.docker.json ]; then
    cp instance/config.docker.json instance/config.json
  elif [ -f instance/config.docker.json.example ]; then
    cp instance/config.docker.json.example instance/config.json
  fi
fi

python - <<'PY'
import json
import os
from pathlib import Path

config_path = Path("instance/config.json")

if config_path.exists():
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"No se pudo leer instance/config.json: {exc}")
else:
    config = {}

db = config.setdefault("db", {})
db["host"] = os.environ.get("GTD_DB_HOST", db.get("host", "mysql"))
db["port"] = int(os.environ.get("GTD_DB_INTERNAL_PORT", db.get("port", 3306)))
db["user"] = os.environ.get("GTD_DB_USER", db.get("user", os.environ.get("MYSQL_USER", "gtd")))
db["password"] = os.environ.get("GTD_DB_PASSWORD", db.get("password", os.environ.get("MYSQL_PASSWORD", "gtd_password")))
db["database"] = os.environ.get("GTD_DB_NAME", db.get("database", os.environ.get("MYSQL_DATABASE", "gtd")))
db["charset"] = db.get("charset", "utf8mb4")

app = config.setdefault("app", {})
if os.environ.get("GTD_TIMEZONE"):
    app["timezone"] = os.environ["GTD_TIMEZONE"]
app.setdefault("timezone", "Europe/Madrid")
if os.environ.get("GTD_APP_TITLE"):
    app["title"] = os.environ["GTD_APP_TITLE"]
app.setdefault("title", "GTD App")

calendar_sync = app.setdefault("calendar_sync", {})
if os.environ.get("GTD_CALENDAR_ID"):
    calendar_sync["calendar_id"] = os.environ["GTD_CALENDAR_ID"]

config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
PY

exec gunicorn --bind 0.0.0.0:5000 wsgi:application
