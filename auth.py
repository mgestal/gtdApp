"""Login con contraseña + TOTP para GTD App (usuario único).

Configuración en instance/config.json, sección "auth" (la crea manage_auth.py):

    "auth": {
      "enabled": true,
      "password_hash": "...",      # werkzeug (scrypt)
      "totp_secret": "BASE32...",  # RFC 6238, SHA1, 6 dígitos, 30 s
      "secret_key": "...",         # clave de firma de la cookie de sesión
      "session_days": 30,
      "cookie_secure": true,
      "notify_telegram": true
    }

Si "enabled" no es true la app funciona como antes (sin login propio).
Si está activado pero falta la contraseña o el secreto TOTP, la app se
bloquea (503) en vez de quedar abierta.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import secrets
import struct
import threading
import time as _time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional
from urllib.parse import quote

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

TOTP_STEP = 30
TOTP_DIGITS = 6
TOTP_WINDOW = 1  # acepta el código anterior y el siguiente (±30 s de desfase)

IP_MAX_FAILURES = 5
IP_WINDOW = timedelta(minutes=15)
GLOBAL_MAX_FAILURES = 20
GLOBAL_WINDOW = timedelta(hours=1)

EXEMPT_ENDPOINTS = {"auth_login", "static", "webmanifest", "service_worker"}
EXEMPT_PATH_PREFIXES = ("/api/extension/",)  # protegidos por token propio

AUTH_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS auth_login_attempts ("
    " id BIGINT AUTO_INCREMENT PRIMARY KEY,"
    " ip VARCHAR(64) NOT NULL,"
    " success TINYINT(1) NOT NULL,"
    " totp_counter BIGINT NULL,"
    " user_agent VARCHAR(255) NULL,"
    " created_at_utc DATETIME NOT NULL,"
    " INDEX idx_auth_attempts_time (created_at_utc),"
    " INDEX idx_auth_attempts_ip (ip, created_at_utc)"
    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
)

_hooks: Dict[str, Callable[..., Any]] = {}
_schema_ready = False


# ---------------------------------------------------------------- TOTP

def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _b32decode(secret: str) -> bytes:
    s = secret.strip().replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def totp_code(secret: str, counter: int, digits: int = TOTP_DIGITS) -> str:
    digest = hmac.new(_b32decode(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** digits)).zfill(digits)


def verify_totp(secret: str, code: str, now: Optional[float] = None,
                window: int = TOTP_WINDOW) -> Optional[int]:
    """Devuelve el contador TOTP que coincide con el código, o None."""
    code = (code or "").strip().replace(" ", "")
    if not secret or len(code) != TOTP_DIGITS or not code.isdigit():
        return None
    current = int((now if now is not None else _time.time()) // TOTP_STEP)
    for counter in range(current - window, current + window + 1):
        if hmac.compare_digest(totp_code(secret, counter), code):
            return counter
    return None


def totp_uri(secret: str, account: str = "usuario", issuer: str = "GTD App") -> str:
    label = quote(f"{issuer}:{account}")
    return (f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}"
            f"&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_STEP}")


def hash_password(password: str) -> str:
    return generate_password_hash(password)


# ---------------------------------------------------------------- config

def get_auth_cfg() -> Dict[str, Any]:
    cfg = _hooks["load_config"]() if "load_config" in _hooks else {}
    auth = cfg.get("auth") if isinstance(cfg, dict) else None
    return auth if isinstance(auth, dict) else {}


def auth_enabled(auth: Dict[str, Any]) -> bool:
    return auth.get("enabled") is True


def auth_fingerprint(auth: Dict[str, Any]) -> str:
    """Cambia si cambian la contraseña o el TOTP: invalida sesiones anteriores."""
    raw = f"{auth.get('password_hash', '')}|{auth.get('totp_secret', '')}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def is_logged_in(auth: Dict[str, Any]) -> bool:
    return session.get("auth_ok") is True and session.get("auth_fp") == auth_fingerprint(auth)


# ---------------------------------------------------------------- BD

def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def ensure_auth_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    _hooks["exec_sql"](AUTH_SCHEMA)
    _hooks["commit"]()
    _schema_ready = True


def count_failures(since: datetime, ip: Optional[str] = None) -> int:
    ensure_auth_schema()
    if ip is None:
        row = _hooks["q1"]("SELECT COUNT(*) AS n FROM auth_login_attempts "
                           "WHERE success=0 AND created_at_utc >= %s", (since,))
    else:
        row = _hooks["q1"]("SELECT COUNT(*) AS n FROM auth_login_attempts "
                           "WHERE success=0 AND ip=%s AND created_at_utc >= %s", (ip, since))
    return int((row or {}).get("n") or 0)


def last_success_counter() -> int:
    ensure_auth_schema()
    row = _hooks["q1"]("SELECT MAX(totp_counter) AS c FROM auth_login_attempts WHERE success=1")
    value = (row or {}).get("c")
    return int(value) if value is not None else -1


def record_attempt(ip: str, success: bool, totp_counter: Optional[int] = None) -> None:
    ensure_auth_schema()
    ua = (request.headers.get("User-Agent") or "")[:255]
    _hooks["exec_sql"](
        "INSERT INTO auth_login_attempts(ip, success, totp_counter, user_agent, created_at_utc) "
        "VALUES(%s,%s,%s,%s,%s)", (ip[:64], 1 if success else 0, totp_counter, ua, _utc_now()))
    _hooks["exec_sql"]("DELETE FROM auth_login_attempts WHERE created_at_utc < %s",
                       (_utc_now() - timedelta(days=90),))
    _hooks["commit"]()


def lockout_reason(ip: str) -> Optional[str]:
    now = _utc_now()
    if count_failures(now - IP_WINDOW, ip) >= IP_MAX_FAILURES:
        return "Demasiados intentos fallidos desde esta conexión. Espera 15 minutos."
    if count_failures(now - GLOBAL_WINDOW) >= GLOBAL_MAX_FAILURES:
        return "Acceso bloqueado temporalmente por demasiados intentos fallidos. Espera una hora."
    return None


# ---------------------------------------------------------------- utilidades

def client_ip() -> str:
    """IP del cliente. Solo confía en X-Forwarded-For si llega desde un proxy local
    (p. ej. Tailscale Funnel -> Apache en localhost)."""
    remote = request.remote_addr or ""
    try:
        is_local = ipaddress.ip_address(remote).is_loopback
    except ValueError:
        is_local = False
    if is_local:
        forwarded = [p.strip() for p in (request.headers.get("X-Forwarded-For") or "").split(",") if p.strip()]
        if forwarded:
            return forwarded[-1]
    return remote or "desconocida"


def safe_next(target: Optional[str]) -> Optional[str]:
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return None
    return target


def notify(text: str) -> None:
    """Aviso por Telegram en segundo plano (sin bloquear ni romper el login)."""
    if get_auth_cfg().get("notify_telegram", True) is False:
        return

    def _send() -> None:
        try:
            from reminders import load_reminder_token, send_telegram
            from telegram_gtdAppbot import load_chat_id
            send_telegram(load_reminder_token(), load_chat_id(), text)
        except Exception:
            pass

    threading.Thread(target=_send, daemon=True).start()


def _wants_json() -> bool:
    if request.path.startswith("/api/"):
        return True
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    best = request.accept_mimetypes.best_match(["text/html", "application/json"])
    return best == "application/json" and request.accept_mimetypes[best] > request.accept_mimetypes["text/html"]


def _csrf_token(key: str) -> str:
    token = session.get(key)
    if not token:
        token = secrets.token_urlsafe(32)
        session[key] = token
    return token


def _check_csrf(key: str) -> bool:
    expected = session.get(key) or ""
    supplied = request.form.get("csrf_token") or ""
    return bool(expected) and hmac.compare_digest(expected, supplied)


# ---------------------------------------------------------------- Flask

def require_login():
    auth = get_auth_cfg()
    g.auth_enabled = auth_enabled(auth)
    if not g.auth_enabled:
        return None
    if request.endpoint in EXEMPT_ENDPOINTS or request.path.startswith(EXEMPT_PATH_PREFIXES):
        return None
    if not auth.get("password_hash") or not auth.get("totp_secret"):
        abort(503, "Login activado pero sin configurar. Ejecuta manage_auth.py setup.")
    if is_logged_in(auth):
        return None
    if request.method == "GET" and not _wants_json():
        return redirect(url_for("auth_login", next=request.full_path.rstrip("?")))
    return jsonify({"error": "login_required"}), 401


def login_view():
    auth = get_auth_cfg()
    if not auth_enabled(auth):
        return redirect(url_for("home"))
    next_url = safe_next(request.values.get("next"))

    if request.method == "GET":
        if is_logged_in(auth):
            return redirect(next_url or url_for("home"))
        return render_template("auth_login.html", next_url=next_url or "",
                               csrf_token=_csrf_token("login_csrf"))

    ip = client_ip()
    status = 401
    if not _check_csrf("login_csrf"):
        flash("La sesión del formulario ha caducado. Vuelve a intentarlo.", "error")
        status = 400
    elif (reason := lockout_reason(ip)):
        flash(reason, "error")
        status = 429
    else:
        password = request.form.get("password") or ""
        code = request.form.get("code") or ""
        pw_ok = bool(auth.get("password_hash")) and check_password_hash(auth["password_hash"], password)
        counter = verify_totp(auth.get("totp_secret", ""), code)
        if pw_ok and counter is not None and counter > last_success_counter():
            record_attempt(ip, True, counter)
            remember = request.form.get("remember") == "1"
            session.clear()
            session["auth_ok"] = True
            session["auth_fp"] = auth_fingerprint(auth)
            session["auth_at"] = int(_time.time())
            session.permanent = remember
            notify(f"🔐 GTD App: inicio de sesión desde {ip}\n{(request.headers.get('User-Agent') or '')[:120]}")
            return redirect(next_url or url_for("home"))
        record_attempt(ip, False)
        if count_failures(_utc_now() - IP_WINDOW, ip) == IP_MAX_FAILURES:
            notify(f"⚠️ GTD App: {IP_MAX_FAILURES} intentos de login fallidos desde {ip}. Bloqueada 15 min.")
        flash("Contraseña o código incorrectos.", "error")

    return render_template("auth_login.html", next_url=next_url or "",
                           csrf_token=_csrf_token("login_csrf")), status


def logout_view():
    if _check_csrf("logout_csrf"):
        session.clear()
    return redirect(url_for("auth_login"))


def init_auth(app: Flask, *, load_config, q1, exec_sql, commit) -> None:
    _hooks.update(load_config=load_config, q1=q1, exec_sql=exec_sql, commit=commit)

    auth = get_auth_cfg()
    # La clave aleatoria de config.json manda sobre GTD_SECRET_KEY del entorno
    # (que puede ser débil, p. ej. la de /etc/apache2/envvars).
    if auth.get("secret_key"):
        app.config["SECRET_KEY"] = auth["secret_key"]
    elif auth_enabled(auth):
        weak = {"", "CHANGE_ME_IN_PROD", "1234567890"}
        if str(app.config.get("SECRET_KEY") or "") in weak or len(str(app.config["SECRET_KEY"])) < 32:
            raise RuntimeError("Login activado con SECRET_KEY débil: ejecuta manage_auth.py setup")
    app.config["SESSION_COOKIE_NAME"] = "gtdapp_session"
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = auth.get("cookie_secure", True) is not False
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=int(auth.get("session_days", 30)))

    app.add_url_rule("/login", "auth_login", login_view, methods=["GET", "POST"])
    app.add_url_rule("/logout", "auth_logout", logout_view, methods=["POST"])
    # Antes que cualquier otro before_request (sincronización de calendario, etc.)
    app.before_request_funcs.setdefault(None, []).insert(0, require_login)

    @app.context_processor
    def _auth_context():
        enabled = getattr(g, "auth_enabled", False)
        return {"auth_enabled": enabled,
                "logout_csrf": _csrf_token("logout_csrf") if enabled and session.get("auth_ok") else ""}
