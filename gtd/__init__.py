"""Paquete de GTD App.

Módulos por capas (cada uno solo importa de los anteriores):

- ``gtd.config``: Configuración (instance/config.json) y lectura de valores.
- ``gtd.utils``: Utilidades sin estado: conversión de valores, fechas, paginación, URLs seguras.
- ``gtd.core``: Objeto Flask ``app`` y filtros Jinja.
- ``gtd.db``: Conexión MySQL por petición y helpers q/q1/exec_sql/commit/rollback.
- ``gtd.schema``: Migraciones de esquema versionadas (``migrations/``, ``migrate.py``).
- ``gtd.lookups``: Búsqueda/creación de etiquetas, tipos, proyectos y carpetas por nombre.
- ``gtd.roles``: Roles GTD: etiquetas/carpetas especiales configurables (ver gtd_roles.py).
- ``gtd.quick_entry``: Parser de entrada rápida (fechas, horas, prioridad, recurrencia, #proyecto, f:carpeta, @etiquetas).
- ``gtd.filter_expr``: Expresiones de filtros guardados: tokenizador, parser y compilación a SQL.
- ``gtd.kanban_board``: Lógica del Kanban global y de los tableros por proyecto (sin rutas).
- ``gtd.recurrence``: Tareas periódicas: RRULE básico, siguiente fecha y registro de ejecuciones.
- ``gtd.gmail``: Integración con Gmail: rutas de credenciales, OAuth de admin y control de correos importados.
- ``gtd.calendar_sync``: Sincronización bidireccional de tareas con Google Calendar (push, pull, conflictos, autosync).
- ``gtd.common``: Consultas compartidas por varias vistas: árbol de carpetas, etiquetas por tarea, subtareas, permisos admin.
- ``gtd.backups``: Copias de seguridad: listado, mysqldump y restauración.
- ``gtd.tasks_ops``: Operaciones sobre tareas reutilizadas por varias rutas (completar, etc.).
- ``gtd.context``: Context processors de plantillas (contadores y barra lateral).
- ``gtd.hooks``: Login (auth.py), before_request (config + migraciones + autosync Calendar), PWA y errores.

Las rutas están en ``gtd.views`` (un módulo por área). ``app.py`` (raíz) importa
``gtd.core.app`` y registra hooks, context processors y vistas.
"""
