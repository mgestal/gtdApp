# wsgi.py
# Punto de entrada para Apache/mod_wsgi

from app import app as application  # mod_wsgi busca "application"
