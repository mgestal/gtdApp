"""Objeto Flask `app` y filtros Jinja (el login se registra en gtd.hooks)."""
from __future__ import annotations

import os
import re

from flask import Flask
from markupsafe import escape as html_escape, Markup

from gtd.config import BASE_DIR
from gtd.utils import format_hhmm


# import_name "app" conserva el nombre del logger; root_path apunta a la raíz (templates/, static/).
app = Flask("app", root_path=str(BASE_DIR), instance_relative_config=True)
app.config["SECRET_KEY"] = os.environ.get("GTD_SECRET_KEY", "CHANGE_ME_IN_PROD")
app.config["GTD_SECRET_FROM_ENV"] = bool(os.environ.get("GTD_SECRET_KEY"))


def _linkify_title(title: str) -> Markup:
    """Convierte URLs y enlaces Markdown en el título de una tarea a hipervínculos HTML.

    Sintaxis soportada:
      - URLs directas: www.ejemplo.com, http://..., https://...
      - Markdown: [Texto mostrado](https://ejemplo.com)
    """
    if not title:
        return Markup(html_escape(title or ""))

    _MD_LINK_RE = re.compile(r'\[([^\]]+)\]\(((?:https?://|www\.)[^\s\)]+)\)')
    _BARE_URL_RE = re.compile(r'(https?://[^\s<>"()\[\]]+|www\.[^\s<>"()\[\]]+)')

    matches: list = []
    for m in _MD_LINK_RE.finditer(title):
        matches.append((m.start(), m.end(), "md", m))

    md_ranges = [(s, e) for s, e, kind, _ in matches]
    for m in _BARE_URL_RE.finditer(title):
        s, e = m.start(), m.end()
        if not any(ms <= s and e <= me for ms, me in md_ranges):
            matches.append((s, e, "url", m))

    matches.sort(key=lambda x: x[0])

    parts: list = []
    last = 0
    for start, end, kind, m in matches:
        parts.append(str(html_escape(title[last:start])))
        if kind == "md":
            text = m.group(1)
            url = m.group(2)
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            parts.append(
                f'<a href="{html_escape(url)}" class="task-url-link" target="_blank" rel="noopener noreferrer">'
                f'{html_escape(text)}</a>'
            )
        else:
            url = m.group(0)
            href = url if url.startswith("http") else "https://" + url
            parts.append(
                f'<a href="{html_escape(href)}" class="task-url-link" target="_blank" rel="noopener noreferrer">'
                f'{html_escape(url)}</a>'
            )
        last = end

    parts.append(str(html_escape(title[last:])))
    return Markup("".join(parts))


app.jinja_env.filters["linkify_title"] = _linkify_title
app.jinja_env.filters["hhmm"] = format_hhmm
