# GTDApp

![Python](https://img.shields.io/badge/python-3.10+-blue)
![Flask](https://img.shields.io/badge/flask-web%20app-green)
![MySQL](https://img.shields.io/badge/database-mysql-orange)
![License](https://img.shields.io/badge/license-personal-lightgrey)

Aplicación personal de **Getting Things Done (GTD)** desarrollada en
**Python + Flask**, pensada para ejecutarse en un servidor propio
(Raspberry Pi, NAS o VPS).

Incluye:

-   gestión completa de tareas
-   proyectos y carpetas jerárquicas
-   etiquetas
-   subtareas
-   lenguaje de filtros avanzado
-   importación de correos desde Gmail
-   importación de eventos desde Google Calendar
-   captura rápida desde Telegram

El objetivo es tener un sistema **GTD completo, ligero y auto‑alojado**.

------------------------------------------------------------------------

# Características

## Gestión de tareas

-   Inbox
-   proyectos
-   carpetas
-   etiquetas
-   subtareas
-   fechas
-   tareas recurrentes

------------------------------------------------------------------------

## Lenguaje de filtros

Permite construir consultas complejas similares a **Todoist / Things**.

### Ejemplos

    @NextAction
    p:Casa
    fa:Trabajo & !done
    inbox & !done

### Prefijos

  Prefijo   Significado
  --------- -----------------------------------
  `@tag`    etiqueta
  `p:`      proyecto
  `f:`      carpeta
  `fr:`     carpeta recursiva
  `fa:`     carpeta + subcarpetas + proyectos
  `pf:`     búsqueda por proyecto/carpeta

### Palabras clave

    done
    inbox

### Operadores

    &  AND
    |  OR
    !  NOT
    () agrupación

------------------------------------------------------------------------

# Integración Gmail

Importa correos automáticamente como tareas en Inbox (añade tag inbox.email)

Características:

-   usa **Gmail API**
-   evita duplicados
-   guarda enlace al correo
-   permite filtrar con queries de Gmail
-   parsea título de proyecto en busqueda de etiquetas, proyectos, fechas...

Ejemplo de query:

    label:ToGTD in:inbox

------------------------------------------------------------------------

# Integración Google Calendar

Importa eventos automáticamente como tareas en Inbox (añade tag inbox.calendar)

Características:

-   usa **Google Calendar API**
-   evita duplicados
-   guarda enlace al evento
-   permite dos tipos importaciones: por fecha de ocurrencia del evento 
                                     o por fecha de creación del evento


------------------------------------------------------------------------

# Integración Telegram

Bot para captura rápida desde el móvil.

Ejemplo:

    Comprar pilas @Casa *hoy

### Formato soportado

    texto libre
    @etiqueta
    *hoy
    *mañana
    dd/mm/aaaa

### Comandos

  Comando          Función
  ---------------- --------------------------------------
  `/info` `/help`  información del bot y ayuda
  `/nextactions`   lista tareas con etiqueta NextAction
  `/hoy`           lista tareas con fecha hoy (resumen)
  `/today`         lista tareas con fecha hoy (extendido)
  `/agenda`        lista tareas agendadas con fecha
  `/done`          lista tareas completadas hoy


### Script send_today.py

   Envía a telegram un listado de las tareas para hoy

------------------------------------------------------------------------

# Instalación rápida

``` bash
git clone https://github.com/usuario/gtdApp.git
cd gtdApp

python -m venv .venv
source .venv/bin/activate

pip install flask pymysql python-telegram-bot google-api-python-client google-auth google-auth-oauthlib google-auth-httplib2
```

------------------------------------------------------------------------

# Configuración

Crear:

    instance/config.json

Ejemplo:

``` json
{
  "db": {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "user",
    "password": "password",
    "database": "gtd",
    "charset": "utf8mb4"
  },
  "app": {
    "timezone": "Europe/Madrid",
    "title": "GTD App",
    "pagination": {
      "agenda_per_page": 15,
      "search_per_page": 20,
      "tags_per_page": 10,
      "tag_detail_per_page": 10,
      "folders_per_page": 10,
      "filters_per_page": 10,
      "projects_per_page": 15
    }
  }
}

```

------------------------------------------------------------------------

# Ejecutar la aplicación

    python app.py

Abrir:

    http://localhost:5000

------------------------------------------------------------------------

# Telegram Bot

Guardar el token para publicación en:

    .token-gtdapp

Arrancar:

    python telegram_bot.py

------------------------------------------------------------------------

# Seguridad

Archivos que **NO subidos al repositorio**:

    instance/config.json               (configuracion acceso BBDD)
    instance/gmail_credentials.json    (GoogleAPI: oauth 2.0 credentials)
    instance/gmail_token.json          (GoogleAPI: autorizacion)
    .token-gtdapp                      (token bot telegram)

Añadidos al `.gitignore`.



------------------------------------------------------------------------

# Estructura del proyecto

    gtdApp/
    │
    ├── app.py
    ├── calendar_import.py
    ├── gmail_import.py
    ├── send_today.py
    ├── subtasks.py
    ├── telegram_bot.py
    │
    ├── templates/
    │
    ├── sql
    |   └── esquema.sql
    │
    ├── static/
    │   ├── style.css
    |   └── tag_autocomplete.js
    │
    ├── instance/
    │   ├── config.json
    │   ├── gmail_credentials.json
    │   ├── gmail_token.json
    |   └── telegram_chat.json
    │
    └── .token-gtdapp

------------------------------------------------------------------------

# Licencia

Uso personal / open source.
