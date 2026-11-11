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
- Tablero Kanban: una tarea puede aparecer simultáneamente en todas las cards cuyos criterios cumpla, además de Inbox cuando no tiene proyecto ni carpeta
- Visibilidad configurable de tareas realizadas y archivadas en los contadores del menú lateral y en las listas de etiquetas

### Sintaxis rápida para crear tareas

```
Llamar a Juan @NextAction *mañana
Comprar tinta #Oficina *18-03
Revisar plan #"Area Personal"
Revisar rutina f:"GTD Folders"
Revisar cuentas @Finanzas *+3 h:09:00 cada mes
Enviar propuesta ^alta @NextAction *hoy
Preparar presentación Miércoles
Llamar a "marcos@udc.es" 20:00
```

Formato: `nombre [@etiqueta] [^alta|^media|^baja] [*fecha | hoy | mañana | lunes|martes|miércoles|jueves|viernes|sábado|domingo] [HH:MM | h:HH:MM] [#proyecto | #"proyecto con espacios"] [f:carpeta | f:"carpeta con espacios"] [cada día|semana|mes|año]`

Las comillas dobles conservan como texto literal su contenido y no se guardan en el título; por ejemplo, `"20:00"` no fija la hora y `"marcos@udc.es"` no crea una etiqueta.

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

### Etiquetas y contadores del menú lateral

- En la cabecera del grupo de etiquetas del menú lateral, activa **Realizadas** para incluir tareas completadas en las listas y contadores de etiquetas, o **Archivo** para incluir tareas archivadas y tareas de proyectos archivados.
- Estas opciones se guardan en la configuración de la aplicación y afectan tanto a la página de detalle de cada etiqueta como al número de tareas mostrado junto a cada etiqueta.
- Por defecto ambas opciones están desactivadas. Las tareas eliminadas siguen excluidas.

### Tablero Kanban

- Cada card muestra todas las tareas activas que cumplen su criterio; una misma tarea puede aparecer en varias cards a la vez.
- Las tareas sin proyecto ni carpeta aparecen también en la card fija Inbox, incluso si coinciden con otras cards.
- Al arrastrar una tarea a una card, se actualizan las etiquetas de estado según el criterio de destino. Las cards coincidentes se actualizan automáticamente.

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

### Recordatorios por Telegram y navegador

En **Editar tarea → Recordatorios** se pueden añadir varios avisos, editarlos o eliminarlos:

- **Fecha y hora concretas:** el aviso es independiente del vencimiento y se envía una sola vez, también en tareas periódicas.
- **Antes del vencimiento:** indica una antelación en minutos, horas o días. Si la tarea no tiene hora, es obligatorio proporcionar una hora de referencia. El aviso se recalcula al cambiar la fecha u hora y para cada nuevo vencimiento de una tarea periódica.

En cada recordatorio se pueden elegir **Telegram, navegador o ambos**. Los nuevos seleccionan ambos canales por defecto; los existentes conservan únicamente Telegram. Si se seleccionan ambos se reciben dos avisos. El canal navegador envía a todos los dispositivos suscritos, no a los dispositivos de la extensión.

Para activar Web Push, abre **Admin → Abrir panel de notificaciones** en cada dispositivo, introduce un nombre, pulsa **Activar en este dispositivo** y acepta el permiso. Desde ese panel puedes enviar una prueba al dispositivo actual o a todos los dispositivos activos, desactivar el dispositivo actual o eliminar otro de la lista. Las pruebas respetan el horario configurado y muestran los errores por dispositivo; la aceptación del proveedor no garantiza su visualización. Requiere HTTPS y un navegador compatible. En iOS/iPadOS se necesita una versión compatible y abrir la aplicación web añadida a la pantalla de inicio. Permisos del sistema, modos de concentración o ajustes de batería pueden impedir que se muestre el aviso. La suscripción puede caducar; en ese caso se indica en Admin y debe volver a activarse en el dispositivo.

El nombre de la instalación y el certificado TLS se configuran en el servidor web o proxy, no en GTDApp. Para Web Push el certificado debe ser válido y de confianza para el nombre utilizado por el navegador. Si cambias de dominio, puerto o ruta, vuelve a activar las notificaciones desde la nueva dirección: las suscripciones pertenecen a cada origen. La URL base opcional del panel (`app.public_url`) se usa solo para añadir enlaces a mensajes de Telegram.

Si usas una CA privada, puedes configurar `app.notifications.ca_certificate_path` en `instance/config.json` con la ruta absoluta de su **certificado público PEM**, legible por el proceso web. El panel de notificaciones ofrecerá su descarga y mostrará la huella SHA-256; no configures ni distribuyas la clave privada. Verifica la huella por un medio independiente y seguro antes de instalar el certificado raíz. Un dispositivo que todavía no confía en HTTPS necesitará recibir el certificado por otro medio seguro; descargarlo mediante una conexión cuya identidad no se ha verificado no establece confianza. Este ajuste es opcional, no instala certificados y no configura TLS en el servidor.

En **Admin → Renovación tokens Google** puedes configurar la URL HTTPS completa del callback OAuth (`app.google_oauth.redirect_uri`); si está vacía, se utiliza la dirección desde la que se accede a Admin. Debe coincidir exactamente con una URL de redirección autorizada en Google Cloud e incluir el prefijo de instalación y `/admin/google_oauth/callback`. Inicia la autorización desde el mismo dominio y puerto para conservar la sesión del navegador. La configuración de esta URL no modifica el certificado TLS.

La clave privada VAPID se crea en `instance/.vapid-private.pem` al activar el primer dispositivo; debe ser legible por el usuario de la aplicación y por el proceso de recordatorios. No se sube a Git. Inclúyela en una copia de seguridad **privada**, junto con la base de datos: perderla invalida las suscripciones existentes. Si falta pero hay dispositivos registrados, se solicita restaurarla o eliminar esas suscripciones antes de crear una nueva. En Docker, `instance/` está compartido por los dos servicios. Tras cambiar las dependencias, reconstruye las imágenes.

La vista previa utiliza la fecha guardada de la tarea y la zona horaria `app.timezone` de la aplicación. Guarda los cambios de la tarea antes de añadir o modificar recordatorios. Las horas inexistentes durante el cambio de horario se rechazan; si una hora se repite, se usa su primera ocurrencia. Las antelaciones se calculan sobre la hora local de la tarea.

La campana en las listas abre los recordatorios de la tarea. Los recordatorios de tareas completadas, archivadas o en la papelera, o de proyectos archivados o eliminados, quedan suspendidos. Si se restaura la tarea, los avisos pendientes vuelven a evaluarse. Si se quita la fecha o la hora necesaria para un aviso relativo, este queda suspendido hasta que se vuelva a informar.

Los envíos solo se realizan dentro del horario configurable en **Admin → Notificaciones**, por defecto de 08:00 a 20:00, ambos minutos incluidos, en la zona horaria de la aplicación. La franja puede cruzar medianoche. También se restringen las pruebas. Un aviso programado fuera de la franja se intentará enviar cuando esta vuelva a abrirse, siempre que tenga menos de 24 horas de retraso.

Para que los avisos se envíen aunque nadie esté usando la web, además de configurar el bot y registrar el chat con `/start`, hay que ejecutar el proceso **cada 15 minutos**. La hora programada indica cuándo queda pendiente el aviso, no garantiza el envío exacto: se procesa en la siguiente revisión dentro de la franja. En una instalación manual, añade al crontab del usuario que puede leer la configuración y el token:

```cron
*/15 * * * * /ruta/gtdApp/.venv/bin/python /ruta/gtdApp/send_reminders.py >> /ruta/gtdApp/recordatorios.log 2>&1
```

En Docker, copia el token del bot a `instance/.token-gtdapp` con permisos restrictivos (esa ruta está excluida de Git), asegúrate de que `instance/telegram_chat.json` contiene el chat registrado e inicia el servicio opcional:

```bash
docker compose --profile reminders up -d --build
docker compose --profile reminders logs -f reminders
```

El servicio usa el mismo `instance/` que la aplicación. El token de `instance/.token-gtdapp` se utiliza para los recordatorios y su prueba en Admin; el bot ejecutado fuera de Docker sigue utilizando el token de la raíz.

El proceso evita envíos duplicados ordinarios mediante un registro por recordatorio, momento y canal; en el navegador también se registra cada dispositivo. Los fallos se reintentan de forma independiente en la siguiente ejecución, pero si un servicio recibe el mensaje y se pierde la respuesta, podría haber duplicados. Si no había dispositivos suscritos, se volverá a intentar hasta que se registre alguno o caduque el aviso; una vez iniciado el envío, se conserva la lista inicial de destinatarios. Tras una caída, solo se procesan avisos con menos de 24 horas de retraso. El servicio push puede aceptar un aviso sin que llegue a verse; se solicita entrega inmediata sin almacenamiento diferido, por lo que un dispositivo desconectado podría no recibirlo. Esto evita que se entregue deliberadamente fuera del horario permitido.

El proceso y sus errores aparecen en **Admin → Notificaciones**. La URL pública opcional se usa para los enlaces de Telegram; al pulsar una notificación del navegador se abre la tarea en la misma instalación de GTDApp. El estado del proceso solo indica su última ejecución, no garantiza la entrega o visualización.

### Avisos por ubicación al abrir GTDApp

Edita una etiqueta, activa **Recordatorio por ubicación** y guarda su latitud, longitud y radio en metros. El botón **Usar ubicación actual** puede rellenar las coordenadas con permiso del navegador; también puedes introducirlas manualmente. Desactivar la ubicación de la etiqueta y guardar elimina sus coordenadas y radio.
Las etiquetas con ubicación configurada se identifican con 📍 en el listado y en el menú lateral.

En cada móvil o portátil, abre **Admin → Notificaciones → Avisos por ubicación** y pulsa **Activar en este dispositivo**. Se necesita HTTPS, un navegador compatible y permiso de ubicación. La activación se guarda en ese navegador; para dejar de usarla pulsa **Desactivar en este dispositivo**. El permiso del navegador se gestiona por separado en sus ajustes.

Al abrir o recargar GTDApp se comprueban las etiquetas con tareas pendientes, dentro del horario de notificaciones configurado. No se comprueba la posición al navegar entre páginas ni al volver a una pestaña abierta en segundo plano. Los avisos aparecen en la campana de la barra superior, no por Telegram ni Web Push, y enlazan a la lista de tareas pendientes de cada etiqueta. Las ubicaciones detectadas se conservan al navegar en la misma pestaña; al pulsar una ubicación se marcan como leídas las tareas que mostraba y solo volverán a contarse tareas nuevas. La lectura de ubicación se guarda en el navegador, independientemente de otros dispositivos.

La comparación se realiza en el dispositivo y su posición no se envía al servidor ni se almacena como historial. Las ubicaciones configuradas de las etiquetas sí se guardan en la base de datos. Para evitar falsos positivos, la zona de incertidumbre indicada por el navegador debe quedar completamente dentro del radio; si la precisión es insuficiente no se mostrará ese aviso. Un portátil sin GPS puede necesitar un radio mayor. Si el permiso se deniega, falla la ubicación o no hay conexión, se informa en la aplicación. La función no detecta llegadas mientras GTDApp está cerrada.

### Campana de notificaciones

La campana agrupa los avisos de ubicación y los recordatorios enviados por Telegram y navegador. El número suma las tareas nuevas por ubicación, sin duplicarlas si coinciden varias etiquetas, y los recordatorios enviados no leídos. Al pulsar un aviso se marca como leído y se abre su etiqueta o tarea; también se pueden marcar todos como leídos.

Los envíos del mismo recordatorio y momento se agrupan, mostrando sus canales y dispositivos. El historial se actualiza al abrir la campana, al volver a la aplicación y cada minuto mientras está visible. La lectura de estos envíos se comparte entre dispositivos, mientras que los avisos de ubicación y su lectura son locales al navegador. El historial conserva visibles los envíos leídos durante el periodo configurado en **Admin → Notificaciones → Historial de la campana** (por defecto, un día). Es una ventana de visualización, no borra el historial. Solo incluye registros de envío todavía disponibles: al borrar un recordatorio o una suscripción también pueden desaparecer sus registros. La aceptación de Telegram o del servicio push no garantiza que el aviso se haya mostrado en el dispositivo.

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
