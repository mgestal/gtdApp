# Extensión Chrome: GTDApp Quick Inbox

## Qué hace

- Muestra tareas de:
  - Hoy
  - Esta semana
  - Próximos 7 días
- Muestra barra vertical de prioridad, fecha, hora, carpeta (📂) y proyecto (💼) de cada tarea.
- Permite marcar/desmarcar tareas con checkbox.
- Añade la página actual del navegador como tarea en Inbox:
  - Texto opcional para reemplazar el enlace como título.
  - Selector de prioridad (Alta / Media / Baja / sin prioridad).
- Usa el mismo icono que la GTDApp.

## Endpoints backend usados

- `GET /api/extension/tasks?scope=today|week|next7`
- `POST /api/extension/tasks/<task_id>/toggle`
- `POST /api/extension/tasks/add_page`

## Cargar en Chrome

1. Ve a `chrome://extensions`.
2. Activa `Modo de desarrollador`.
3. Pulsa `Cargar descomprimida`.
4. Selecciona la carpeta `chrome_extension`.

## Configuración

- En el popup, define `URL base GTDApp` (por ejemplo `http://localhost:5000` o tu dominio).
- Pulsa `Guardar`.

## Notas

- La extensión usa cookies de sesión (`credentials: include`) para llamar a tu backend.
- Si tu instancia está detrás de login/sesión, asegúrate de estar autenticado en esa URL en el navegador.
- Las tareas en la lista muestran la carpeta si está asignada directamente a la tarea o al proyecto.
- La prioridad elegida al añadir página al Inbox se guarda en la tarea (1=Alta, 2=Media, 3=Baja).
