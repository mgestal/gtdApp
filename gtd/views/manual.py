"""Manual de usuario integrado."""
from __future__ import annotations

from flask import redirect, render_template, url_for

from gtd.core import app


@app.route("/import")
def import_view():
    return render_template("import.html")


@app.route("/manual")
def manual_legacy():
    return redirect(url_for("manual"), code=302)


@app.route("/gtdApp/manual")
def manual():
    return render_template("manual/index.html", title="Manual de usuario")


@app.route("/gtdApp/manual/filtros")
def manual_filters():
    return render_template("manual/filters.html", title="Manual: Filtros")


@app.route("/gtdApp/manual/kanban")
def manual_kanban():
    return render_template("manual/kanban.html", title="Manual: Kanban")


@app.route("/gtdApp/manual/gmail")
def manual_gmail():
    return render_template("manual/gmail.html", title="Manual: Integración con Gmail")


@app.route("/gtdApp/manual/google-calendar")
def manual_google_calendar():
    return render_template("manual/google_calendar.html", title="Manual: Integración con Google Calendar")


@app.route("/gtdApp/manual/telegram")
def manual_telegram():
    return render_template("manual/telegram.html", title="Manual: Integración con Telegram")


@app.route("/gtdApp/manual/despliegue")
def manual_deployment():
    return render_template("manual/deployment.html", title="Manual: Instalación y despliegue")
