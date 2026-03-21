# GTDApp

![Python](https://img.shields.io/badge/python-3.10+-blue)
![Flask](https://img.shields.io/badge/flask-3.x-green)
![MariaDB](https://img.shields.io/badge/database-mariadb-orange)
![License](https://img.shields.io/badge/license-personal-lightgrey)

Aplicación personal de **Getting Things Done (GTD)** desarrollada en **Python + Flask**, pensada para ejecutarse en un servidor propio (Raspberry Pi, NAS o VPS).

> Sistema GTD completo, ligero y auto-alojado.

---

## Características

### Gestión de tareas

- Inbox, proyectos, carpetas jerárquicas, etiquetas, subtareas
- Fechas de vencimiento, hora y recurrencias (diaria, semanal, mensual, anual)
- **Histórico de ejecuciones** de tareas periódicas accesible directamente desde el chip ⟲ periódica
- Archivar tareas realizadas desde el Inbox con un solo clic
- Revisión semanal con bloque EnEspera separado en tareas etiquetadas y proyectos de la carpeta EnEspera
- Creación rápida desde cualquier vista con sintaxis natural
- Vistas: Inbox, Hoy, Esta semana, NextActions, Próximo, Calendario

### Sintaxis rápida para crear tareas

```
Llamar a Juan @NextAction *mañana
Comprar tinta #Oficina *18-03
Revisar cuentas @Finanzas *+3 h:09:00 cada mes
```

Formato: `nombre [@etiqueta] [*fecha] [h:HH:MM] [#proyecto] [cada día|semana|mes|año]`

### Lenguaje de filtros avanzado

Filtros guardados con expresiones similares a Todoist / Things.

| Prefijo | Significado |
|---------|-------------|
| `@tag` | etiqueta exacta |
| `p:nombre` | proyecto exacto (`p:null` → sin proyecto) |
| `f:nombre` | carpeta directa (`f:null` → sin carpeta) |
| `fr:nombre` | carpeta directa + subcarpetas |
| `fa:nombre` | carpeta anywhere (tarea o proyecto) |
| `pf:valor` | búsqueda libre en proyecto/carpeta |
| `fecha<hoy` | comparación de fecha (`fecha`/`due` + `<` `<=` `=` `>=` `>`) |

Palabras clave: `inbox`, `done`, `hoy`, `null`  
Operadores: `&` (AND), `|` (OR), `!` (NOT), `( )` (agrupación). También se acepta `and`/`or`.

**Ejemplos:**
```
@NextAction & !done & due<=+3
fa:Trabajo & (@Urgente | @Agenda)
inbox | p:null
fecha<hoy
```

### Sincronización Google Calendar (bidireccional)

- GTD → Google: crea/actualiza eventos al sincronizar desde Admin
- Las tareas sin fecha y sin hora no se envían a Google Calendar
- Google → GTD: propaga cambios de título, fecha y hora a la tarea vinculada
- Resolución de conflictos desde el panel Admin
- Borrado remoto: si se elimina un evento en Google, la tarea se archiva en GTD
- Calendario objetivo configurable (`app.calendar_sync.calendar_id` en `config.json`)

### Importación Gmail

- Importa correos como tareas en Inbox (tag `inbox.gmail`)
- Evita duplicados; guarda enlace al mensaje original
- Filtra con queries de Gmail (`label:ToGTD`, `in:inbox`, etc.)
- Parsea etiquetas, proyecto y fecha del asunto del correo

### Importación Google Calendar → Inbox

- Importa eventos futuros como tareas (tag `inbox.calendar`)
- Evita duplicados; guarda enlace al evento

### Bot Telegram

Captura rápida desde el móvil:

```
Llamar a Juan @NextAction *mañana
Comprar pilas @Casa *hoy h:20:00
```

**Comandos disponibles:**

| Comando | Función |
|---------|---------|
| `/help` `/info` | Información y ayuda |
| `/nextactions` | Tareas con etiqueta NextAction |
| `/hoy` | Resumen de tareas para hoy |
| `/today` | Lista extendida de tareas para hoy |
| `/agenda` | Tareas agendadas con fecha |
| `/done` | Tareas completadas hoy |

**`send_today.py`** envía el listado diario al chat configurado (apto para cron).

---

## Stack técnico

| Componente | Versión |
|-----------|---------|
| Python | 3.10+ (probado en 3.13) |
| Flask | 3.x |
| PyMySQL | 1.1.x |
| MariaDB / MySQL | 10.6+ |
| python-telegram-bot | 21.x |
| google-api-python-client | 2.x |
| Servidor producción | Apache 2.4 + mod_wsgi |

---

## Instalación

### Opción A: Docker (Recomendado)

La forma más sencilla de ejecutar la app en cualquier equipo.

**Requisitos:** Docker y Docker Compose

```bash
git clone https://github.com/usuario/gtdApp.git
cd gtdApp

# Iniciar la app (crea automáticamente BD y volúmenes)
docker-compose up -d

# La app está disponible en http://localhost:5000
```

**Comandos útiles:**

```bash
# Ver logs
docker-compose logs -f app

# Parar
docker-compose down

# Parar y eliminar datos (BD)
docker-compose down -v
```

**Configuración:** Edita `instance/config.docker.json` antes de iniciar.

---

### Opción B: Instalación manual

### 1. Clonar el repositorio

```bash
git clone https://github.com/usuario/gtdApp.git
cd gtdApp
```

### 2. Entorno virtual y dependencias

```bash
python -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

### 3. Crear la base de datos

```bash
mysql -u root -p <<'SQL'
CREATE DATABASE gtd CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'gtd'@'localhost' IDENTIFIED BY 'your_password';
GRANT ALL PRIVILEGES ON gtd.* TO 'gtd'@'localhost';
FLUSH PRIVILEGES;
SQL

mysql -u gtd -p gtd < sql/esquema.sql
```

### 4. Configuración

Crea `instance/config.json`:

```json
{
  "db": {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "gtd",
    "password": "your_password",
    "database": "gtd",
    "charset": "utf8mb4"
  },
  "app": {
    "timezone": "Europe/Madrid",
    "title": "GTD App",
    "calendar_sync": {
      "calendar_id": "primary"
    },
    "pagination": {
      "agenda_per_page": 15,
      "search_per_page": 20,
      "tags_per_page": 10,
      "tag_detail_per_page": 10,
      "folders_per_page": 10,
      "filters_per_page": 10,
      "projects_per_page": 15,
      "nextactions_per_page": 25,
      "archive_tasks_per_page": 25,
      "archive_projects_per_page": 25,
      "periodic_history_per_page": 20
    }
  },
  "security": {
    "allowed_user_ids": [],
    "allowed_group_ids": [],
    "reply_on_unauthorized": true
  }
}
```

> `calendar_sync.calendar_id`: usa `"primary"` para el calendario principal de la cuenta, o el email de un calendario compartido (p. ej. `"usuario@gmail.com"`).

> `security.allowed_user_ids`: lista de Telegram User IDs autorizados a usar el bot.

---

## Integración Google (Gmail + Calendar)

### 1. Credenciales OAuth

1. En [Google Cloud Console](https://console.cloud.google.com/), crear un proyecto y habilitar las APIs **Gmail** y **Google Calendar**.
2. Crear un OAuth 2.0 Client ID (tipo *Desktop app* para flujo manual).
3. Descargar el JSON y guardarlo como `instance/gmail_credentials.json`.

### 2. Generar el token de autorización

```bash
python auth_generate_token.py
```

El script imprime una URL de autorización. Tras conceder permisos, el token se guarda en `instance/gmail_token.json`.

> En servidores sin navegador (Raspberry Pi, VPS), copia la URL en otro equipo, autoriza y pega el código resultante de vuelta en el terminal.

### 3. Permisos en producción (Apache/mod_wsgi)

```bash
sudo chown www-data:www-data instance/gmail_token.json
sudo chmod 660 instance/gmail_token.json
```

---

## Bot Telegram

### 1. Crear el bot

1. Habla con [@BotFather](https://t.me/BotFather) y crea un nuevo bot.
2. Copia el token y guárdalo en `.token-gtdapp` (raíz del proyecto).

### 2. Configurar usuarios autorizados

En `instance/config.json`, rellena `security.allowed_user_ids` con los Telegram User IDs permitidos.

### 3. Arrancar el bot

```bash
python telegram_gtdAppbot.py
```

El bot guarda el chat activo en `instance/telegram_chat.json` cuando un usuario autorizado lo inicia.

### 4. Resumen diario automático (cron)

```
30 7 * * * /ruta/venv/bin/python /ruta/gtdApp/send_today.py
```

---

## Ejecutar en desarrollo

```bash
source .venv/bin/activate
python app.py
```

Abre `http://localhost:5000`.

---

## Despliegue en producción (Apache + mod_wsgi)

```apache
<VirtualHost *:80>
    ServerName gtdapp.local
    WSGIDaemonProcess gtdapp user=www-data group=www-data threads=2
    WSGIScriptAlias / /var/www/gtdApp/wsgi.py
    <Directory /var/www/gtdApp>
        WSGIProcessGroup gtdapp
        WSGIApplicationGroup %{GLOBAL}
        Require all granted
    </Directory>
</VirtualHost>
```

```bash
sudo a2ensite gtdapp
sudo systemctl reload apache2
```

---

## Estructura del proyecto

```
gtdApp/
├── app.py                    # Aplicación Flask principal
├── calendar_import.py        # Importación Google Calendar
├── gmail_import.py           # Importación Gmail
├── send_today.py             # Envío resumen diario por Telegram
├── subtasks.py               # Gestión de subtareas
├── telegram_gtdAppbot.py     # Bot de Telegram
├── wsgi.py                   # Entry point para Apache/mod_wsgi
│
├── templates/
│   ├── base.html
│   ├── home.html
│   ├── manual/               # Manual de usuario integrado en la app
│   │   ├── index.html
│   │   ├── filters.html
│   │   ├── gmail.html
│   │   ├── google_calendar.html
│   │   └── telegram.html
│   └── ...
│
├── static/
│   ├── style.css
│   ├── tag_autocomplete.js
│   └── review_filters.html   # Referencia de filtros para la revisión semanal
│
├── sql/
│   └── esquema.sql           # Esquema de base de datos
│
├── instance/                 # Configuración local — NO versionada
│   ├── config.json
│   ├── gmail_credentials.json
│   ├── gmail_token.json
│   └── telegram_chat.json
│
└── .token-gtdapp             # Token del bot Telegram — NO versionado
```

---

## Ficheros excluidos del repositorio

Los siguientes ficheros contienen credenciales y **no deben subirse a Git**:

```
instance/config.json
instance/gmail_credentials.json
instance/gmail_token.json
instance/telegram_chat.json
.token-gtdapp
```

Comprueba que están listados en `.gitignore`.

---

## Manual de usuario

La aplicación incluye un manual integrado accesible desde el icono **❓** del menú superior, o directamente en `/gtdApp/manual`.

---

## Licencia

Uso personal. Sin licencia de distribución abierta.
