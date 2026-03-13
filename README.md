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

Importa correos automáticamente como tareas en Inbox.

Características:

-   usa **Gmail API**
-   evita duplicados
-   guarda enlace al correo
-   permite filtrar con queries de Gmail

Ejemplo de query:

    label:ToGTD in:inbox

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
  `/nextactions`   lista tareas con etiqueta NextAction
  `/hoy`           lista tareas con fecha hoy

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
    "user": "gtd",
    "password": "password",
    "database": "gtd",
    "charset": "utf8mb4"
  },
  "app": {
    "timezone": "Europe/Madrid",
    "title": "GTD App"
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

Guardar el token en:

    .token-gtdapp

Arrancar:

    python telegram_bot.py

------------------------------------------------------------------------

# Seguridad

Archivos que **NO deben subirse al repositorio**:

    instance/config.json
    instance/gmail_credentials.json
    instance/gmail_token.json
    .token-gtdapp

Añadirlos al `.gitignore`.

------------------------------------------------------------------------

# Estructura del proyecto

    gtdApp/
    │
    ├── app.py
    ├── gmail_import.py
    ├── telegram_bot.py
    ├── subtasks.py
    │
    ├── templates/
    ├── static/
    │
    ├── instance/
    │   ├── config.json
    │   ├── gmail_credentials.json
    │   └── gmail_token.json
    │
    └── .token-gtdapp

------------------------------------------------------------------------

# Roadmap

-   completar tareas desde Telegram
-   notificaciones Telegram
-   adjuntos en tareas
-   OCR de documentos
-   integración calendario
-   API REST

------------------------------------------------------------------------

# Licencia

Uso personal / open source.
