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
- Prioridades opcionales por tarea: Alta, Media, Baja o sin prioridad
- Resaltado visual con barra vertical de color en listados de trabajo (cuando la prioridad está informada)
- Fechas de vencimiento, hora y recurrencias (diaria, semanal, mensual, anual)
- **Histórico de ejecuciones** de tareas periódicas accesible directamente desde el chip ⟲ periódica
- Al completar una periódica caducada, la app permite elegir entre mantener la siguiente fecha calculada o saltar a la primera fecha válida posterior a hoy
- Papelera para tareas y proyectos eliminados, con restauración y vaciado desde Admin
- Archivar tareas realizadas desde el Inbox con un solo clic
- Revisión semanal con bloque EnEspera separado en tareas etiquetadas y proyectos de la carpeta EnEspera
- En el detalle de proyecto, el icono `⚡` del título "Tareas del proyecto" muestra por hover el estado de promoción automática de NextAction
- En el detalle de proyecto, las tareas activas se pueden reordenar por arrastre; ese orden se usa para la promoción automática de NextAction
- Creación rápida desde cualquier vista con sintaxis natural
- Vistas: Inbox, Hoy, Esta semana, NextActions, Próximo, Calendario

### Sintaxis rápida para crear tareas

```
Llamar a Juan @NextAction *mañana
Comprar tinta #Oficina *18-03
Revisar plan #"Area Personal"
Revisar rutina f:"GTD Folders"
Revisar cuentas @Finanzas *+3 h:09:00 cada mes
Enviar propuesta ^alta @NextAction *hoy
```

Formato: `nombre [@etiqueta] [^alta|^media|^baja] [*fecha] [h:HH:MM] [#proyecto | #"proyecto con espacios"] [f:carpeta | f:"carpeta con espacios"] [cada día|semana|mes|año]`

Notas sobre prioridad:

- El token `^alta`, `^media` o `^baja` asigna prioridad al crear la tarea rápida.
- Si no se indica prioridad, la tarea queda sin prioridad (`NULL`).

Notas sobre tareas periódicas caducadas:

- Al marcar como realizada una tarea periódica cuyo siguiente vencimiento quedaría en el pasado, aparece un modal con tres opciones.
- `Cancelar`: no marca la tarea.
- `Mantener fecha`: usa la siguiente fecha calculada por la regla, aunque siga en pasado.
- `Usar fecha válida`: salta automáticamente al primer vencimiento posterior a hoy.

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
| `prioridad:valor` | prioridad exacta (`alta`, `media`, `baja`, `1`, `2`, `3`, `null`) |
| `fecha<hoy` | comparación de fecha (`fecha`/`due` + `<` `<=` `=` `>=` `>`) |

Referencias de fecha admitidas en comparadores: `hoy`, `null`, `N`, `+N`, `-N`, `dd-mm-aaaa`.

Palabras clave: `inbox`, `done`, `hoy`, `null`  
Operadores: `&` (AND), `|` (OR), `!` (NOT), `( )` (agrupación). También se acepta `and`/`or`.

**Ejemplos:**
```
@NextAction & !done & due<=+3
due>=+7
fa:Trabajo & (@Urgente | @Agenda)
inbox | p:null
fecha<hoy
prioridad:alta & !done
```

### Sincronización Google Calendar (bidireccional)

- GTD → Google: crea/actualiza eventos al sincronizar desde Admin
- Las tareas sin fecha y sin hora no se envían a Google Calendar
- Google → GTD: propaga cambios de título, fecha y hora a la tarea vinculada
- Resolución de conflictos en vista dedicada: Admin -> Ir a conflictos (`/calendar/conflicts`)
- Los conflictos se generan solo cuando hay cambios concurrentes en campos sincronizados: título, notas y fecha/hora
- Marcar una tarea como realizada/no realizada o archivada/no archivada no genera conflicto
- Cambios locales de carpeta, proyecto o etiquetas no generan conflicto
- Borrado remoto: si se elimina un evento en Google, la tarea se archiva en GTD
- Calendario objetivo configurable (`app.calendar_sync.calendar_id` en `config.json`)

### Papelera

- Al borrar una tarea o proyecto, se envía a papelera en lugar de eliminarse definitivamente.
- Los elementos en papelera no aparecen en búsquedas, filtros, enlaces de etiquetas ni archivo.
- Desde Admin puedes:
  - Restaurar tareas o proyectos individualmente.
  - Borrar definitivamente tareas eliminadas hace más de 7 días.
  - Vaciar toda la papelera.

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

# Crear tu configuración de entorno
cp .env.example .env

# Iniciar la app (crea automáticamente BD y volúmenes)
docker compose up -d

# La app está disponible en http://localhost:5000
```

Si no existe `instance/config.json`, el contenedor la genera automáticamente usando estas variables y, si están presentes, `instance/config.docker.json` o `instance/config.docker.json.example` como base.

Si el host ya usa esos puertos, puedes cambiarlos sin editar el YAML:

```bash
GTD_APP_PORT=5001 GTD_DB_PORT=3307 docker compose up -d
```

**Comandos útiles:**

```bash
# Ver logs
docker compose logs -f app

# Parar
docker compose down

# Parar y eliminar datos (BD)
docker compose down -v
```

**Configuración:** Para un servidor nuevo, ajusta `.env` y, si quieres personalizar más campos de la app, crea `instance/config.docker.json` a partir de `instance/config.docker.json.example` antes de iniciar.

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

Si estás actualizando una instalación existente (sin recrear la base de datos), aplica esta migración para habilitar el orden manual en proyectos:

```sql
ALTER TABLE tasks ADD COLUMN sort_order INT NULL DEFAULT NULL;
ALTER TABLE tasks ADD KEY idx_tasks_project_sort (project_id, sort_order);

UPDATE tasks t
JOIN (
  SELECT id,
         ROW_NUMBER() OVER (PARTITION BY project_id ORDER BY (due_date IS NULL) ASC, due_date ASC, id ASC) AS rn
  FROM tasks
  WHERE project_id IS NOT NULL
) x ON x.id=t.id
SET t.sort_order=x.rn
WHERE t.sort_order IS NULL;
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

### 5. Configurar Apache + mod_wsgi (manual avanzado)

Si vas a publicar la app en `/gtdApp`, puedes usar una configuración como esta.

#### HTTP (`000-default.conf`)

```apache
# -------------------------
# GTD App
# -------------------------
WSGIDaemonProcess gtdApp user=www-data group=www-data threads=5 \
  python-home=/var/www/gtdApp/.venv \
  python-path=/var/www/gtdApp

WSGIScriptAlias /gtdApp /var/www/gtdApp/wsgi.py process-group=gtdApp application-group=%{GLOBAL}

# Admin interno de la app
SetEnv GTD_ADMIN_PASSWORD "N03m1t4"
SetEnv GTD_SECRET_KEY "1234567890"

Alias /gtdApp/static /var/www/gtdApp/static
<Directory /var/www/gtdApp>
  AuthType Basic
  AuthName "Restricted Content"
  AuthUserFile /etc/apache2/htpasswd-apacheusers
  Require valid-user
  Options FollowSymLinks
</Directory>

<Directory /var/www/gtdApp/static>
  AuthType Basic
  AuthName "Restricted Content"
  AuthUserFile /etc/apache2/htpasswd-apacheusers
  Require valid-user
  Options FollowSymLinks
</Directory>
```

#### SSL (`apps-SSL.conf`)

```apache
# ========================
# GTD App (en el mismo vhost SSL)
# ========================
WSGIDaemonProcess gtdAppSSL user=www-data group=www-data threads=5 \
  python-home=/var/www/gtdApp/.venv \
  python-path=/var/www/gtdApp

WSGIScriptAlias /gtdApp /var/www/gtdApp/wsgi.py process-group=gtdAppSSL application-group=%{GLOBAL}

Alias /gtdApp/static /var/www/gtdApp/static
<Directory /var/www/gtdApp>
  AuthType Basic
  AuthName "Restricted Content"
  AuthUserFile /etc/apache2/htpasswd-apacheusers
  Require valid-user
  Options FollowSymLinks
</Directory>
<Directory /var/www/gtdApp/static>
  AuthType Basic
  AuthName "Restricted Content"
  AuthUserFile /etc/apache2/htpasswd-apacheusers
  Require valid-user
  Options FollowSymLinks
</Directory>

# Variables de entorno para la app
SetEnv GTD_ADMIN_PASSWORD "gtd_password"
SetEnv GTD_SECRET_KEY "1234567890"
```

Activación y recarga:

```bash
sudo a2enmod wsgi ssl
sudo a2ensite 000-default apps-SSL
sudo systemctl reload apache2
```

Recomendación: no dejes contraseñas reales en los ficheros de vhost; usa secretos en variables de entorno del sistema o un mecanismo seguro equivalente.

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

## SQLTools en VS Code (Remote/SSH)

Si usas la extensión SQLTools conectándote por SSH al servidor, necesita runtime de Node.js en el host remoto.

Síntoma habitual:

- En SQLTools aparece "No connections found" y el botón "Add New Connection" no responde.
- En la terminal de VS Code se ve `node: command not found` en la sesión `detect node runtime`.

Solución:

```bash
sudo apt-get install -y --fix-missing nodejs
sudo ln -sf "$(which nodejs)" /usr/local/bin/node
node --version
```

Después, recarga la ventana de VS Code con `Developer: Reload Window`.

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
