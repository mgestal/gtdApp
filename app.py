"""GTD App: punto de entrada.

``wsgi.py`` (Apache/mod_wsgi) y Docker (``gunicorn wsgi:application``) importan ``app`` de aquí.
El código vive en el paquete ``gtd/`` (ver ``gtd/__init__.py``).
"""
from gtd.core import app
from gtd import context, hooks, views  # noqa: F401  registran context processors, hooks y rutas

__all__ = ["app"]

if __name__ == "__main__":  # desarrollo: python app.py → http://localhost:5000
    app.run(debug=True)
