#!/usr/bin/env python3
"""Gestión del login (contraseña + TOTP) de GTD App.

Uso (desde /var/www/gtdApp):
    .venv/bin/python manage_auth.py setup      # contraseña + TOTP + clave de sesión, y activa el login
    .venv/bin/python manage_auth.py password   # cambia la contraseña
    .venv/bin/python manage_auth.py totp       # genera un nuevo secreto TOTP (nuevo QR)
    .venv/bin/python manage_auth.py enable | disable | status
    .venv/bin/python manage_auth.py unlock     # borra los intentos fallidos (desbloquea)

Tras cualquier cambio: touch wsgi.py  (Apache recarga la app).
Cambiar contraseña o TOTP cierra todas las sesiones abiertas.
"""
from __future__ import annotations

import getpass
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from auth import hash_password, new_totp_secret, totp_uri, verify_totp  # noqa: E402

CONFIG_PATH = BASE_DIR / "instance" / "config.json"
MIN_PASSWORD = 12


def load() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def save(cfg: dict) -> None:
    # Escribir en el mismo fichero conserva propietario y permisos
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
    mode = CONFIG_PATH.stat().st_mode
    if mode & stat.S_IROTH:
        print(f"\n⚠️  {CONFIG_PATH} es legible por cualquier usuario del sistema.\n"
              f"   Recomendado: sudo chmod 660 {CONFIG_PATH}")


def ask_password() -> str:
    while True:
        pw = getpass.getpass(f"Nueva contraseña (mín. {MIN_PASSWORD} caracteres): ")
        if len(pw) < MIN_PASSWORD:
            print("Demasiado corta.")
            continue
        if getpass.getpass("Repítela: ") != pw:
            print("No coinciden.")
            continue
        return pw


def enroll_totp() -> str:
    secret = new_totp_secret()
    uri = totp_uri(secret)
    print("\nEscanea este QR con tu app de autenticación (Aegis, Google Authenticator, 1Password...):\n")
    if shutil.which("qrencode"):
        subprocess.run(["qrencode", "-t", "ANSIUTF8", "-m", "2", uri], check=False)
    else:
        print("(qrencode no está instalado; usa la clave manual)")
    print(f"\nO añádela a mano. Clave: {' '.join(secret[i:i + 4] for i in range(0, len(secret), 4))}")
    print("Tipo: basado en tiempo (TOTP), 6 dígitos, 30 s\n")
    for _ in range(5):
        code = input("Introduce el código que muestra la app para comprobarlo: ")
        if verify_totp(secret, code, now=time.time()) is not None:
            print("✅ Código correcto.")
            return secret
        print("Código incorrecto. ¿Está bien la hora del móvil? Prueba otra vez.")
    sys.exit("No se ha verificado el TOTP; no se guarda nada.")


def auth_section(cfg: dict) -> dict:
    auth = cfg.setdefault("auth", {})
    auth.setdefault("session_days", 30)
    auth.setdefault("cookie_secure", True)
    auth.setdefault("notify_telegram", True)
    if not auth.get("secret_key"):
        auth["secret_key"] = secrets.token_urlsafe(48)
    return auth


def cmd_setup(cfg: dict) -> None:
    auth = auth_section(cfg)
    auth["password_hash"] = hash_password(ask_password())
    auth["totp_secret"] = enroll_totp()
    auth["enabled"] = True
    save(cfg)
    print("\nLogin configurado y activado. Ejecuta: touch wsgi.py")


def cmd_password(cfg: dict) -> None:
    auth = auth_section(cfg)
    auth["password_hash"] = hash_password(ask_password())
    save(cfg)
    print("Contraseña cambiada. Ejecuta: touch wsgi.py")


def cmd_totp(cfg: dict) -> None:
    auth = auth_section(cfg)
    auth["totp_secret"] = enroll_totp()
    save(cfg)
    print("TOTP regenerado (el anterior deja de valer). Ejecuta: touch wsgi.py")


def cmd_enable(cfg: dict, value: bool) -> None:
    auth = auth_section(cfg)
    if value and not (auth.get("password_hash") and auth.get("totp_secret")):
        sys.exit("Falta contraseña o TOTP: ejecuta primero 'setup'.")
    auth["enabled"] = value
    save(cfg)
    print(("Login activado." if value else "Login DESACTIVADO.") + " Ejecuta: touch wsgi.py")


def cmd_status(cfg: dict) -> None:
    auth = cfg.get("auth") or {}
    print(f"Activado:        {auth.get('enabled') is True}")
    print(f"Contraseña:      {'sí' if auth.get('password_hash') else 'no'}")
    print(f"TOTP:            {'sí' if auth.get('totp_secret') else 'no'}")
    print(f"Clave de sesión: {'sí' if auth.get('secret_key') else 'no'}")
    print(f"Sesión (días):   {auth.get('session_days', 30)}")
    print(f"Avisos Telegram: {auth.get('notify_telegram', True)}")


def cmd_unlock(cfg: dict) -> None:
    import pymysql
    db = cfg.get("db", {})
    conn = pymysql.connect(host=db.get("host"), port=int(db.get("port", 3306)), user=db.get("user"),
                           password=db.get("password"), database=db.get("database"),
                           charset=db.get("charset", "utf8mb4"))
    with conn, conn.cursor() as cur:
        cur.execute("DELETE FROM auth_login_attempts WHERE success=0")
        print(f"Borrados {cur.rowcount} intentos fallidos.")
        conn.commit()


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    cfg = load()
    actions = {
        "setup": lambda: cmd_setup(cfg),
        "password": lambda: cmd_password(cfg),
        "totp": lambda: cmd_totp(cfg),
        "enable": lambda: cmd_enable(cfg, True),
        "disable": lambda: cmd_enable(cfg, False),
        "status": lambda: cmd_status(cfg),
        "unlock": lambda: cmd_unlock(cfg),
    }
    if cmd not in actions:
        sys.exit(__doc__)
    if not os.access(CONFIG_PATH, os.W_OK) and cmd not in ("status", "unlock"):
        sys.exit(f"No tienes permiso de escritura en {CONFIG_PATH}")
    actions[cmd]()


if __name__ == "__main__":
    main()
