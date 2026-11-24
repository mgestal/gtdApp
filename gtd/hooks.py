"""Login (auth.py), before_request (config, esquema, autosync de Calendar), PWA y errores."""
from __future__ import annotations

import json

from flask import g, render_template, request, send_file

from auth import init_auth

from gtd.config import BASE_DIR, load_config
from gtd.core import app
from gtd.db import commit, exec_sql, q1
from gtd.schema import ensure_schema_updates
from gtd.calendar_sync import maybe_run_calendar_autosync


init_auth(app, load_config=lambda: load_config(),
          q1=lambda *a: q1(*a), exec_sql=lambda *a: exec_sql(*a), commit=lambda: commit())


@app.before_request
def _before():
    g.cfg = load_config()
    ensure_schema_updates()
    maybe_run_calendar_autosync()


@app.route("/manifest.webmanifest")
def webmanifest():
    cfg = load_config()
    app_title = cfg.get("app", {}).get("title", "GTD App")
    script_root = (request.script_root or "").rstrip("/")
    base = script_root if script_root else ""

    start_url = f"{base}/pwa-launch" if base else "/pwa-launch"

    manifest = {
        "name": "GTD App",
        "short_name": "GTD App",
        "id": f"{base}/" if base else "/",
        "start_url": start_url,
        "scope": f"{base}/" if base else "/",
        "display": "standalone",
        "display_override": ["standalone", "minimal-ui"],
        "background_color": "#fff7f7",
        "theme_color": "#e7a7ad",
        "lang": "es",
        "description": "GTD App para gestionar tareas y proyectos.",
        "icons": [
            {
                "src": f"{base}/static/icons/emoji-icon-192.png?v=3",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": f"{base}/static/icons/emoji-icon-512.png?v=3",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any",
            },
        ],
    }

    response = app.response_class(
        json.dumps(manifest, ensure_ascii=False),
        mimetype="application/manifest+json",
    )
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/sw.js")
def service_worker():
    sw_path = BASE_DIR / "static" / "sw.js"
    response = send_file(sw_path, mimetype="application/javascript")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/pwa-launch", strict_slashes=False)
def pwa_launch():
    return render_template("pwa_launch.html")


@app.errorhandler(404)
def not_found(e):
    return render_template("error.html", code=404, msg="No encontrado."), 404

@app.errorhandler(500)
def server_error(e):
    return render_template("error.html", code=500, msg="Error interno."), 500
