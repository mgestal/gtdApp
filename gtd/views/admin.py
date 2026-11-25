"""Panel de administración: configuración, backups, consola SQL, tokens API y OAuth de Google."""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from flask import flash, jsonify, redirect, render_template, request, session, url_for

import gtd_roles as _gtd_roles_mod
from reminders import (
    load_reminder_token,
    notification_hours,
    send_telegram,
    utc_now,
    within_notification_hours,
)

from gtd.config import BACKUP_DIR, load_config, save_config
from gtd.utils import normalize_tag_key, safe_backup_filename
from gtd.core import app
from gtd.db import commit, db, exec_sql, get_db_conn, q, q1, rollback
from gtd.roles import ensure_review_defaults, role_tag_sql_names
from gtd.kanban_board import _kanban_subcard_definitions
from gtd.gmail import _build_admin_google_flow, gmail_credentials_path, gmail_token_path
from gtd.common import admin_required, get_api_token_owner_user_id
from gtd.backups import (
    list_archive_task_backups,
    list_backups,
    run_mysql_import_from_file,
    run_mysqldump_to_file,
)
from gtd.views.trash_archive import (
    _handle_archive_action,
    _handle_trash_action,
    _load_trash_view_data,
)
from gtd.views.calendar_admin import _handle_calendar_conflicts_action
from gtd.role_alerts import ROLE_KEYS


def create_api_token(user_id: int, device_name: str) -> str:
    """Crea un token API único y lo guarda en la tabla api_tokens."""
    token = str(uuid.uuid4())
    now = datetime.now()
    exec_sql(
        """
        INSERT INTO api_tokens (user_id, device_name, token, created_at, active)
        VALUES (%s, %s, %s, %s, 1)
        """,
        (user_id, device_name, token, now)
    )
    commit()
    return token

def revoke_api_token(token: str) -> None:
    """Revoca (desactiva) un token API."""
    exec_sql(
        "UPDATE api_tokens SET active=0 WHERE token=%s",
        (token,)
    )
    commit()


# --- Card de gestión de tokens API para extensiones ---
@app.route("/admin/api_tokens", methods=["GET"])
def admin_api_tokens():
    if not admin_required():
        return redirect(url_for("admin"))
    user_id = get_api_token_owner_user_id()
    tokens = q(
        "SELECT id, device_name, token, created_at, last_used_at, active FROM api_tokens WHERE user_id=%s ORDER BY created_at DESC",
        (user_id,)
    )
    # Prefijo token para mostrar solo los primeros caracteres
    for t in tokens:
        t["token_prefix"] = t["token"][:6]
    return render_template("admin_api_tokens.html", tokens=tokens)


def google_oauth_redirect_uri(cfg: Dict[str, Any]) -> str:
    configured = (cfg.get("app", {}).get("google_oauth", {}).get("redirect_uri") or "").strip()
    uri = configured or url_for("admin_google_oauth_callback", _external=True)
    parsed = urlparse(uri)
    expected_path = url_for("admin_google_oauth_callback")
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path != expected_path):
        raise ValueError(f"La URL OAuth debe usar HTTPS y terminar en {expected_path}.")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("El puerto de la URL OAuth no es válido.") from exc
    if parsed.netloc.lower() != request.host.lower():
        raise ValueError("Accede a Admin usando el mismo dominio y puerto configurados para OAuth.")
    return uri


@app.route("/admin", methods=["GET", "POST"])
def admin():
    env_pwd_set = bool(os.environ.get("GTD_ADMIN_PASSWORD", ""))
    api_tokens = []

    if request.method == "POST":
        action = request.form.get("action", "")

        if action == "sync_calendar_now":
            handled = _handle_calendar_conflicts_action(action, "admin")
            if handled is not None:
                return handled

        if action == "backup_db":
               if not admin_required():
                   flash("No autorizado.", "error")
                   return redirect(url_for("admin"))

               BACKUP_DIR.mkdir(parents=True, exist_ok=True)
               ts = datetime.now().strftime("%Y%m%d_%H%M%S")
               fname = f"gtd_backup_{ts}.sql"
               path = BACKUP_DIR / fname

               ok, msg = run_mysqldump_to_file(path)
               flash(f"Copia creada: {fname}" if ok else f"No se pudo crear la copia: {msg}", "ok" if ok else "error")
               return redirect(url_for("admin"))

        if action == "restore_db":
              if not admin_required():
                  flash("No autorizado.", "error")
                  return redirect(url_for("admin"))

              fname = safe_backup_filename(request.form.get("backup_file", ""))
              if not fname:
                flash("Nombre de copia inválido.", "error")
                return redirect(url_for("admin"))

              path = BACKUP_DIR / fname
              if not path.exists():
                flash("La copia seleccionada no existe.", "error")
                return redirect(url_for("admin"))

              # Confirmación explícita (doble)
              if request.form.get("confirm", "") != "YES":
                flash("Restauración cancelada: debes confirmar escribiendo YES.", "error")
                return redirect(url_for("admin"))

              ok, msg = run_mysql_import_from_file(path)
              flash(f"Base de datos restaurada desde {fname}." if ok else f"No se pudo restaurar: {msg}", "ok" if ok else "error")
              return redirect(url_for("admin"))

        if action == "login":
            supplied = request.form.get("password", "")
            if env_pwd_set and supplied == os.environ.get("GTD_ADMIN_PASSWORD"):
                session["is_admin"] = True
                flash("Acceso admin concedido.", "ok")
            else:
                flash("Contraseña incorrecta o GTD_ADMIN_PASSWORD no definida.", "error")
            return redirect(url_for("admin"))

        if not admin_required():
            flash("No autorizado.", "error")
            return redirect(url_for("admin"))

        if action == "test_reminder_telegram":
            from telegram_gtdAppbot import load_chat_id
            try:
                notification_cfg = load_config().get("app", {})
                start, end = notification_hours(notification_cfg)
                zone = ZoneInfo(notification_cfg.get("timezone", "Europe/Madrid"))
                if not within_notification_hours(utc_now(), zone, start, end):
                    flash("La prueba no se envía fuera del horario de notificaciones.", "error")
                    return redirect(url_for("admin_notifications"))
                send_telegram(load_reminder_token(), load_chat_id(), "🔔 Prueba de recordatorios de GTDApp.")
                flash("Mensaje de prueba enviado a Telegram.", "ok")
            except Exception as exc:
                app.logger.warning("La prueba de Telegram falló: %s", type(exc).__name__)
                flash(f"No se pudo enviar la prueba de Telegram ({type(exc).__name__}). Comprueba la configuración y la conexión.", "error")
            return redirect(url_for("admin_notifications"))

        if action == "save_reminder_url":
            public_url = (request.form.get("public_url") or "").strip().rstrip("/")
            parsed = urlparse(public_url)
            if public_url and (parsed.scheme not in ("http", "https") or not parsed.netloc
                               or parsed.username or parsed.password or parsed.query or parsed.fragment):
                flash("Indica una URL pública HTTP o HTTPS válida, sin credenciales ni parámetros.", "error")
            else:
                cfg = load_config()
                cfg.setdefault("app", {})["public_url"] = public_url
                save_config(cfg)
                flash("URL para recordatorios guardada.", "ok")
            return redirect(url_for("admin_notifications"))

        if action == "save_reminder_hours":
            start_raw = (request.form.get("start_time") or "").strip()
            end_raw = (request.form.get("end_time") or "").strip()
            try:
                if not re.fullmatch(r"\d{2}:\d{2}", start_raw) or not re.fullmatch(r"\d{2}:\d{2}", end_raw):
                    raise ValueError("Indica las horas en formato HH:MM.")
                notification_hours({"notifications": {"start_time": start_raw, "end_time": end_raw}})
                cfg = load_config()
                settings = cfg.setdefault("app", {}).setdefault("notifications", {})
                settings["start_time"] = start_raw
                settings["end_time"] = end_raw
                save_config(cfg)
                flash("Horario de recordatorios guardado.", "ok")
            except ValueError as exc:
                flash(f"Horario no válido: {exc}", "error")
            return redirect(url_for("admin_notifications"))

        if action == "save_role_alerts":
            cfg = load_config()
            notifications = cfg.setdefault("app", {}).setdefault("notifications", {})
            for role in ROLE_KEYS:
                time_raw = (request.form.get(f"{role}_time") or "").strip()
                if not re.fullmatch(r"\d{2}:\d{2}", time_raw):
                    flash("Indica la hora de los avisos en formato HH:MM.", "error")
                    return redirect(url_for("admin_notifications") + "#avisos-etiquetas")
                notifications[role] = {
                    "enabled": request.form.get(f"{role}_enabled") == "1",
                    "time": time_raw,
                    **{channel: request.form.get(f"{role}_{channel}") == "1" for channel in ("telegram", "browser", "app")},
                }
            save_config(cfg)
            flash("Avisos automáticos guardados.", "ok")
            return redirect(url_for("admin_notifications") + "#avisos-etiquetas")

        if action == "save_notification_history":
            raw = (request.form.get("history_days") or "").strip()
            try:
                if not re.fullmatch(r"\d+", raw) or not 1 <= int(raw) <= 365:
                    raise ValueError("Indica un número entero entre 1 y 365.")
                cfg = load_config()
                cfg.setdefault("app", {}).setdefault("notifications", {})["history_days"] = int(raw)
                save_config(cfg)
                flash("Historial de notificaciones actualizado.", "ok")
            except ValueError as exc:
                flash(f"Historial no válido: {exc}", "error")
            return redirect(url_for("admin_notifications") + "#historial")

        if action == "save_google_oauth_redirect":
            redirect_uri = (request.form.get("google_oauth_redirect_uri") or "").strip()
            cfg = load_config()
            try:
                candidate = {"app": {"google_oauth": {"redirect_uri": redirect_uri}}}
                if redirect_uri:
                    google_oauth_redirect_uri(candidate)
                cfg.setdefault("app", {}).setdefault("google_oauth", {})["redirect_uri"] = redirect_uri
                save_config(cfg)
                flash("URL de OAuth guardada.", "ok")
            except ValueError as exc:
                flash(f"URL de OAuth no válida: {exc}", "error")
            return redirect(url_for("admin") + "#google-tokens")

        if action == "create_api_token":
            user_id = get_api_token_owner_user_id()
            device_name = (request.form.get("device_name") or "").strip() or "Navegador"
            try:
                token = create_api_token(user_id, device_name)
                flash(f"Token creado para {device_name}: {token}", "ok")
            except Exception as e:
                rollback()
                flash(f"No se pudo crear el token: {e}", "error")
            return redirect(url_for("admin"))

        if action == "revoke_api_token":
            token = (request.form.get("token") or "").strip()
            if not token:
                flash("Token inválido.", "error")
                return redirect(url_for("admin"))

            owned_token = q1(
                "SELECT id FROM api_tokens WHERE token=%s AND user_id=%s",
                (token, get_api_token_owner_user_id()),
            )
            if not owned_token:
                flash("El token no existe o no pertenece al usuario actual.", "error")
                return redirect(url_for("admin"))

            try:
                revoke_api_token(token)
                flash("Token revocado.", "ok")
            except Exception as e:
                rollback()
                flash(f"No se pudo revocar el token: {e}", "error")
            return redirect(url_for("admin"))

        if action == "save_config":
            cfg = load_config()
            cfg["db"]["host"] = request.form.get("db_host", cfg["db"]["host"])
            cfg["db"]["port"] = int(request.form.get("db_port", cfg["db"]["port"]))
            cfg["db"]["user"] = request.form.get("db_user", cfg["db"]["user"])
            cfg["db"]["password"] = request.form.get("db_password", cfg["db"]["password"])
            cfg["db"]["database"] = request.form.get("db_database", cfg["db"]["database"])
            
            # App / Pagination
            cfg.setdefault("app", {})
            cfg["app"].setdefault("pagination", {})

            def _read_int(name: str, fallback: int, min_v: int = 1, max_v: int = 500) -> int:
                raw = (request.form.get(name) or "").strip()
                try:
                    v = int(raw)
                except Exception:
                    v = fallback
                if v < min_v:
                    v = min_v
                if v > max_v:
                    v = max_v
                return v

            # ✅ Agenda (lo que pediste)
            cur_agenda = int(cfg["app"]["pagination"].get("agenda_per_page", 25) or 5)
            cfg["app"]["pagination"]["agenda_per_page"] = _read_int("agenda_per_page", cur_agenda, min_v=5, max_v=500)

            # (Opcional, por si ya quieres dejarlo cableado)
            cfg["app"]["pagination"]["search_per_page"] = _read_int(
                "search_per_page",
                int(cfg["app"]["pagination"].get("search_per_page", 25) or 25),
                min_v=5, max_v=500
            )
            cfg["app"]["pagination"]["tags_per_page"] = _read_int(
                "tags_per_page",
                int(cfg["app"]["pagination"].get("tags_per_page", 12) or 12),
                min_v=5, max_v=200
            )
            cfg["app"]["pagination"]["tag_detail_per_page"] = _read_int(
                "tag_detail_per_page",
                int(cfg["app"]["pagination"].get("tag_detail_per_page", 50) or 50),
                min_v=5, max_v=500
            )
            cfg["app"]["pagination"]["folders_per_page"] = _read_int(
                "folders_per_page",
                int(cfg["app"]["pagination"].get("folders_per_page", 10) or 10),
                min_v=5, max_v=200
            )
            cfg["app"]["pagination"]["filters_per_page"] = _read_int(
                "filters_per_page",
                int(cfg["app"]["pagination"].get("filters_per_page", 15) or 15),
                min_v=5, max_v=500
            )

            cfg["app"]["pagination"]["projects_per_page"] = _read_int(
                "projects_per_page",
                int(cfg["app"]["pagination"].get("projects_per_page", 15) or 15),
                min_v=5, max_v=500
            )
            
            save_config(cfg)
            flash("Configuración guardada.", "ok")
            return redirect(url_for("admin"))

        if action == "renew_google_token":
            creds_path = gmail_credentials_path()
            if not creds_path.exists():
                flash(
                    "Falta instance/gmail_credentials.json. Descarga credenciales OAuth y colócalas en instance/.",
                    "error",
                )
                return redirect(url_for("admin"))

            try:
                redirect_uri = google_oauth_redirect_uri(load_config())
                flow = _build_admin_google_flow(redirect_uri)
                auth_url, state = flow.authorization_url(
                    access_type="offline",
                    prompt="consent",
                    include_granted_scopes="true",
                )
                session["google_oauth_state"] = state
                session["google_oauth_code_verifier"] = getattr(flow, "code_verifier", None)
                session["google_oauth_redirect_uri"] = redirect_uri
                return redirect(auth_url)
            except Exception as e:
                flash(f"No se pudo iniciar la renovación OAuth: {e}", "error")
                return redirect(url_for("admin"))


        if action in ("move_kanban_subcard_up", "move_kanban_subcard_down"):
            cfg = load_config()
            subcards = _kanban_subcard_definitions()
            key = (request.form.get("subcard_key") or "").strip()
            index = next((i for i, sc in enumerate(subcards) if sc["key"] == key), None) if key else None
            if index is None:
                flash("Selecciona una subcard existente para moverla.", "error")
                return redirect(url_for("admin"))
            target = index - 1 if action.endswith("_up") else index + 1
            if 0 <= target < len(subcards):
                subcards[index], subcards[target] = subcards[target], subcards[index]
                cfg.setdefault("kanban", {})["subcards"] = subcards
                save_config(cfg)
            return redirect(url_for("admin", subcard=key))

        if action in ("save_kanban_subcard", "delete_kanban_subcard"):
            cfg = load_config()
            subcards = _kanban_subcard_definitions()
            key = (request.form.get("subcard_key") or "").strip()
            index = next((i for i, sc in enumerate(subcards) if sc["key"] == key), None) if key else None

            if action == "delete_kanban_subcard":
                if index is None:
                    flash("Selecciona una subcard existente para borrarla.", "error")
                else:
                    removed = subcards.pop(index)
                    cfg.setdefault("kanban", {})["subcards"] = subcards
                    save_config(cfg)
                    flash(f"Subcard «{removed['label']}» borrada.", "ok")
                return redirect(url_for("admin"))

            label = (request.form.get("subcard_label") or "").strip()
            folder_name = (request.form.get("subcard_folder_name") or "").strip()
            tag_key = (request.form.get("subcard_tag_key") or "").strip().lstrip("@")
            if not label:
                flash("El nombre de la subcard es obligatorio.", "error")
                return redirect(url_for("admin"))
            if not folder_name and not tag_key:
                flash(f"La subcard «{label}» necesita una carpeta raíz o una tag_key.", "error")
                return redirect(url_for("admin"))

            if index is None:
                used_keys = {sc["key"] for sc in subcards}
                base_key = re.sub(r"[^a-z0-9]+", "-", normalize_tag_key(label)).strip("-") or "subcard"
                new_key, suffix = base_key, 2
                while new_key in used_keys:
                    new_key = f"{base_key}-{suffix}"
                    suffix += 1
                key = new_key
                subcards.append({"key": key, "label": label, "folder_name": folder_name, "tag_key": tag_key})
            else:
                subcards[index].update({"label": label, "folder_name": folder_name, "tag_key": tag_key})

            cfg.setdefault("kanban", {})["subcards"] = subcards
            save_config(cfg)
            flash(f"Subcard «{label}» guardada.", "ok")
            return redirect(url_for("admin", subcard=key))

        if action == "save_gtd_roles":
            cfg = load_config()
            section: Dict[str, Dict[str, List[str]]] = {"tags": {}, "folders": {}}
            for kind, prefix, defs in (
                ("tags", "tag__", _gtd_roles_mod.TAG_ROLES),
                ("folders", "folder__", _gtd_roles_mod.FOLDER_ROLES),
            ):
                for role, defaults, _label, _desc in defs:
                    names = _gtd_roles_mod.parse_names(request.form.get(prefix + role, ""))
                    if names and names != list(defaults):
                        section[kind][role] = names
            if section["tags"] or section["folders"]:
                cfg["gtd_roles"] = section
            else:
                cfg.pop("gtd_roles", None)
            save_config(cfg)
            flash("Etiquetas y carpetas GTD guardadas.", "ok")
            return redirect(url_for("admin") + "#gtd-roles")

        if action == "save_nextaction_behavior":
            cfg = load_config()
            appcfg = cfg.setdefault("app", {})
            behavior = appcfg.setdefault("behavior", {})

            raw = (request.form.get("promote_nextaction_on_complete") or "1").strip().lower()
            behavior["promote_nextaction_on_complete"] = raw not in ("0", "false", "no", "off")

            save_config(cfg)
            flash("Ajuste de NextAction guardado.", "ok")
            return redirect(url_for("admin"))

        if action == "initialize_system":
            if not admin_required():
                flash("No autorizado.", "error")
                return redirect(url_for("admin"))

            try:
                ensure_review_defaults()
                commit()
                flash("Sistema inicializado: etiquetas y carpetas creadas.", "ok")
            except Exception as e:
                rollback()
                flash(f"No se pudo inicializar el sistema: {e}", "error")

            return redirect(url_for("admin"))
        
        if action == "purge_unused_tags":
            if not admin_required():
                flash("No autorizado.", "error")
                return redirect(url_for("admin"))

            try:
                exec_sql(
                    "DELETE tg FROM tags tg "
                    "LEFT JOIN task_tags tt ON tt.tag_id = tg.id "
                    "WHERE tt.tag_id IS NULL"
                )
                commit()
                flash("Etiquetas no usadas eliminadas.", "ok")
            except Exception as e:
                rollback()
                flash(f"No se pudieron eliminar las etiquetas no usadas: {e}", "error")

            return redirect(url_for("admin"))
        
        if action == "purge_empty_projects":
            if not admin_required():
                flash("No autorizado.", "error")
                return redirect(url_for("admin"))

            try:
                exec_sql(
                    "UPDATE projects p "
                    "LEFT JOIN tasks t ON t.project_id = p.id AND t.deleted_at IS NULL "
                    "SET p.deleted_prev_archived=p.archived, p.deleted_at=NOW(), p.archived=1, p.archived_at=COALESCE(p.archived_at, NOW()), p.updated_at=NOW() "
                    "WHERE t.id IS NULL AND p.deleted_at IS NULL"
                )
                commit()
                flash("Proyectos vacíos enviados a papelera.", "ok")
            except Exception as e:
                rollback()
                flash(f"No se pudieron enviar a papelera los proyectos vacíos: {e}", "error")

            return redirect(url_for("admin"))

        handled_trash = _handle_trash_action(action, "admin")
        if handled_trash is not None:
            return handled_trash

        handled_calendar = _handle_calendar_conflicts_action(action, "admin")
        if handled_calendar is not None:
            return handled_calendar

        handled_archive = _handle_archive_action(action, "admin")
        if handled_archive is not None:
            return handled_archive
            
        if action == "purge_completed_tasks":
            if not admin_required():
                flash("No autorizado.", "error")
                return redirect(url_for("admin"))

            days_raw = (request.form.get("older_than_days") or "").strip()
            allowed_days = {"7", "15", "30", "365"}

            if days_raw not in allowed_days:
                flash("Valor de antigüedad inválido.", "error")
                return redirect(url_for("admin"))

            days = int(days_raw)
            periodic_names = tuple(role_tag_sql_names("periodic"))
            periodic_ph = ",".join(["LOWER(%s)"] * len(periodic_names))

            try:
                exec_sql(
                    "UPDATE tasks t "
                    "SET t.deleted_prev_archived=t.archived, t.deleted_at=NOW(), t.archived=1, t.archived_at=COALESCE(t.archived_at, NOW()) "
                    "WHERE t.completed_at IS NOT NULL "
                    "AND t.deleted_at IS NULL "
                    "AND t.completed_at < (NOW() - INTERVAL %s DAY) "
                    "AND (t.recurrence_rule IS NULL OR TRIM(t.recurrence_rule)='') "
                    "AND NOT EXISTS ("
                    "  SELECT 1 FROM task_tags tt "
                    "  JOIN tags tg ON tg.id=tt.tag_id "
                    "  WHERE tt.task_id=t.id "
                    "  AND LOWER(tg.name) IN (" + periodic_ph + ")"
                    ")",
                    (days, *periodic_names),
                )

                exec_sql(
                    "UPDATE tasks t "
                    "SET t.calendar_sync_state='pending_delete', t.calendar_local_changed_at=NOW() "
                    "WHERE t.completed_at IS NOT NULL "
                    "AND t.deleted_at IS NOT NULL "
                    "AND t.google_event_id IS NOT NULL "
                    "AND t.completed_at < (NOW() - INTERVAL %s DAY) "
                    "AND (t.recurrence_rule IS NULL OR TRIM(t.recurrence_rule)='') "
                    "AND NOT EXISTS ("
                    "  SELECT 1 FROM task_tags tt "
                    "  JOIN tags tg ON tg.id=tt.tag_id "
                    "  WHERE tt.task_id=t.id "
                    "  AND LOWER(tg.name) IN (" + periodic_ph + ")"
                    ")",
                    (days, *periodic_names),
                )

                commit()
                flash(f"Tareas realizadas con antigüedad superior a {days} días enviadas a papelera.", "ok")
            except Exception as e:
                rollback()
                flash(f"No se pudieron enviar a papelera las tareas realizadas: {e}", "error")

            return redirect(url_for("admin"))

        if action == "archive_completed_orphans":
            if not admin_required():
                flash("No autorizado.", "error")
                return redirect(url_for("admin"))
            periodic_names = tuple(role_tag_sql_names("periodic"))
            periodic_ph = ",".join(["LOWER(%s)"] * len(periodic_names))

            try:
                total_row = q1(
                    "SELECT COUNT(*) AS c "
                    "FROM tasks t "
                    "WHERE t.archived=0 "
                    "AND t.deleted_at IS NULL "
                    "AND t.project_id IS NULL "
                    "AND t.completed_at IS NOT NULL "
                    "AND t.completed_at < (NOW() - INTERVAL 7 DAY) "
                    "AND (t.recurrence_rule IS NULL OR TRIM(t.recurrence_rule)='') "
                    "AND NOT EXISTS ("
                    "  SELECT 1 FROM task_tags tt "
                    "  JOIN tags tg ON tg.id=tt.tag_id "
                    "  WHERE tt.task_id=t.id "
                    "  AND LOWER(tg.name) IN (" + periodic_ph + ")"
                    ")",
                    periodic_names,
                )
                total_to_archive = int(total_row["c"]) if total_row else 0

                if total_to_archive > 0:
                    exec_sql(
                        "UPDATE tasks "
                        "SET archived=1, archived_at=NOW() "
                        "WHERE id IN ("
                        "  SELECT id FROM ("
                        "    SELECT t.id "
                        "    FROM tasks t "
                        "    WHERE t.archived=0 "
                        "    AND t.deleted_at IS NULL "
                        "    AND t.project_id IS NULL "
                        "    AND t.completed_at IS NOT NULL "
                        "    AND t.completed_at < (NOW() - INTERVAL 7 DAY) "
                        "    AND (t.recurrence_rule IS NULL OR TRIM(t.recurrence_rule)='') "
                        "    AND NOT EXISTS ("
                        "      SELECT 1 FROM task_tags tt "
                        "      JOIN tags tg ON tg.id=tt.tag_id "
                        "      WHERE tt.task_id=t.id "
                        "      AND LOWER(tg.name) IN (" + periodic_ph + ")"
                        "    )"
                        "  ) AS eligible"
                        ")",
                        periodic_names,
                    )
                commit()
                flash(f"{total_to_archive} tareas archivadas.", "ok")
            except Exception as e:
                rollback()
                flash(f"No se pudieron archivar las tareas: {e}", "error")

            return redirect(url_for("admin"))

        if action == "test_db":
            ok = False
            err = ""
            try:
                c = get_db_conn()
                with c.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
                c.close()
                ok = True
            except Exception as e:
                err = str(e)
            flash("Conexión OK." if ok else f"Falló la conexión: {err}", "ok" if ok else "error")
            return redirect(url_for("admin"))
            
            
        if action == "save_pagination":
            cfg = load_config()
            appcfg = cfg.setdefault("app", {})
            pag = appcfg.setdefault("pagination", {})

            def _int_field(name: str, default: int, minv: int, maxv: int) -> int:
                raw = (request.form.get(name) or "").strip()
                try:
                    v = int(raw)
                except ValueError:
                    return default
                if v < minv:
                    return minv
                if v > maxv:
                    return maxv
                return v

            pag["agenda_per_page"] = _int_field("agenda_per_page", pag.get("agenda_per_page", 50), 5, 500)
            pag["search_per_page"] = _int_field("search_per_page", pag.get("search_per_page", 50), 5, 500)
            pag["tags_per_page"] = _int_field("tags_per_page", pag.get("tags_per_page", 12), 5, 200)
            pag["tag_detail_per_page"] = _int_field("tag_detail_per_page", pag.get("tag_detail_per_page", 50), 5, 500)
            pag["folders_per_page"] = _int_field("folders_per_page", pag.get("folders_per_page", 10), 5, 200)
            pag["filters_per_page"] = _int_field("filters_per_page", pag.get("filters_per_page", 15), 5, 500)
            pag["projects_per_page"] = _int_field("projects_per_page", pag.get("projects_per_page", 15), 5, 500)
            pag["nextactions_per_page"] = _int_field("nextactions_per_page", pag.get("nextactions_per_page", 25), 5, 500)
            pag["periodic_history_per_page"] = _int_field("periodic_history_per_page", pag.get("periodic_history_per_page", 20), 5, 500)
            # Archivo: claves separadas para tareas y proyectos (manteniendo compatibilidad con archive_per_page).
            legacy_archive = _int_field("archive_per_page", pag.get("archive_per_page", 25), 5, 500)
            pag["archive_tasks_per_page"] = _int_field(
                "archive_tasks_per_page",
                pag.get("archive_tasks_per_page", legacy_archive),
                5,
                500,
            )
            pag["archive_projects_per_page"] = _int_field(
                "archive_projects_per_page",
                pag.get("archive_projects_per_page", legacy_archive),
                5,
                500,
            )

            save_config(cfg)
            flash("Paginación guardada.", "ok")
            return redirect(url_for("admin"))

    cfg = load_config()
    archive_orphans_preview = []
    trash_counts = {"tasks": 0, "projects": 0}
    archive_counts = {"tasks": 0, "projects": 0}
    calendar_sync_stats = {
        "pending_push": 0,
        "pending_delete": 0,
        "conflict": 0,
        "error": 0,
    }
    notification_status = None

    # Estado del token para la card (unificado)
    token_path = gmail_token_path()
    google_token_status = "unknown"
    google_token_expiry = None
    if token_path.exists():
        try:
            with open(token_path, "r", encoding="utf-8") as f:
                token_data = json.load(f)
                google_token_expiry = token_data.get("expiry")
        except Exception:
            google_token_expiry = None
        from google.oauth2.credentials import Credentials
        creds = Credentials.from_authorized_user_file(str(token_path), [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/calendar"
        ])
        if creds and creds.valid:
            google_token_status = "ok"
        elif creds and creds.expired and creds.refresh_token:
            google_token_status = "refreshing"
        else:
            google_token_status = "expired"

    if admin_required():
        periodic_names = tuple(role_tag_sql_names("periodic"))
        periodic_ph = ",".join(["LOWER(%s)"] * len(periodic_names))
        api_tokens = q(
            "SELECT id, device_name, token, created_at, last_used_at, active FROM api_tokens WHERE user_id=%s ORDER BY created_at DESC",
            (get_api_token_owner_user_id(),),
        )
        for token_row in api_tokens:
            token_row["token_prefix"] = (token_row.get("token") or "")[:6]

        archive_orphans_preview = q(
            "SELECT t.id, t.title, t.completed_at, f.name AS folder_name "
            "FROM tasks t "
            "LEFT JOIN folders f ON f.id=t.folder_id "
            "WHERE t.archived=0 "
            "AND t.deleted_at IS NULL "
            "AND t.project_id IS NULL "
            "AND t.completed_at IS NOT NULL "
            "AND (t.recurrence_rule IS NULL OR TRIM(t.recurrence_rule)='') "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM task_tags tt "
            "  JOIN tags tg ON tg.id=tt.tag_id "
            "  WHERE tt.task_id=t.id "
            "  AND LOWER(tg.name) IN (" + periodic_ph + ")"
            ") "
            "AND t.completed_at < (NOW() - INTERVAL 7 DAY) "
            "ORDER BY t.completed_at ASC, t.id ASC "
            "LIMIT 200",
            periodic_names,
        )

        trash_counts = _load_trash_view_data(task_page=1, project_page=1)["trash_counts"]
        archive_counts_row = q1(
            "SELECT "
            "(SELECT COUNT(*) FROM tasks WHERE archived=1 AND deleted_at IS NULL) AS tasks, "
            "(SELECT COUNT(*) FROM projects WHERE archived=1 AND deleted_at IS NULL) AS projects"
        )
        if archive_counts_row:
            archive_counts = {
                "tasks": int(archive_counts_row.get("tasks") or 0),
                "projects": int(archive_counts_row.get("projects") or 0),
            }

        stats_rows = q(
            "SELECT calendar_sync_state, COUNT(*) AS c "
            "FROM tasks "
            "WHERE calendar_sync_state IN ('pending_push','pending_delete','conflict','error') "
            "GROUP BY calendar_sync_state"
        )
        for r in stats_rows:
            st = r.get("calendar_sync_state")
            if st in calendar_sync_stats:
                calendar_sync_stats[st] = int(r.get("c") or 0)

    calendar_sync_last_info = session.pop("calendar_sync_last_info", None)
    calendar_sync_last_level = session.pop("calendar_sync_last_level", "ok")
    google_oauth_auth_url = session.get("google_oauth_auth_url")

    return render_template(
        "admin.html",
        cfg=cfg,
        admin_panel_order=_admin_panel_order(cfg),
        gtd_role_defs={"tags": _gtd_roles_mod.TAG_ROLES, "folders": _gtd_roles_mod.FOLDER_ROLES},
        gtd_roles_current=_gtd_roles_mod.resolve_roles(cfg),
        kanban_subcards=_kanban_subcard_definitions(),
        selected_kanban_subcard=request.args.get("subcard", ""),
        is_admin=admin_required(),
        api_tokens=api_tokens,
        env_pwd_set=env_pwd_set,
        backups=list_backups(),
        archive_task_backups=list_archive_task_backups(),
        archive_orphans_preview=archive_orphans_preview,
        trash_counts=trash_counts,
        archive_counts=archive_counts,
        calendar_sync_stats=calendar_sync_stats,
        calendar_sync_last_info=calendar_sync_last_info,
        calendar_sync_last_level=calendar_sync_last_level,
        google_token_status=google_token_status,
        google_token_expiry=google_token_expiry,
        notification_status=notification_status,
    )


ADMIN_PANEL_KEYS = (
    "notifications",
    "database",
    "trash",
    "archive",
    "backups",
    "initialize",
    "nextaction",
    "gtd-roles",
    "kanban-subcards",
    "sql",
    "pagination",
    "maintenance",
    "calendar",
    "google-tokens",
    "browser-extension",
)


def _admin_panel_order(cfg: Optional[Dict[str, Any]] = None) -> List[str]:
    cfg = cfg or load_config()
    admin_cfg = cfg.get("admin") or {}
    configured_order = admin_cfg.get("panel_order", [])
    if not isinstance(configured_order, list):
        configured_order = []
    valid_order = []
    for key in configured_order:
        if key in ADMIN_PANEL_KEYS and key not in valid_order:
            valid_order.append(key)
    return valid_order + [key for key in ADMIN_PANEL_KEYS if key not in valid_order]


@app.route("/admin/panel_order", methods=["POST"])
def admin_panel_order():
    if not admin_required():
        return jsonify({"ok": False, "error": "No autorizado"}), 403

    payload = request.get_json(silent=True)
    order = payload.get("order") if isinstance(payload, dict) else None
    if (
        not isinstance(order, list)
        or len(order) != len(ADMIN_PANEL_KEYS)
        or any(not isinstance(key, str) for key in order)
        or set(order) != set(ADMIN_PANEL_KEYS)
    ):
        return jsonify({"ok": False, "error": "El orden de las secciones no es válido"}), 400

    cfg = load_config()
    cfg.setdefault("admin", {})["panel_order"] = order
    save_config(cfg)
    return jsonify({"ok": True})


@app.route("/admin/google_oauth/callback")
def admin_google_oauth_callback():
    """Callback automático de Google OAuth. Recibe code y state, intercambia por token."""
    error = (request.args.get("error") or "").strip()
    if error:
        flash(f"OAuth denegado o cancelado: {error}", "error")
        return redirect(url_for("admin"))

    try:
        expected_state = session.pop("google_oauth_state", None)
        code_verifier = session.pop("google_oauth_code_verifier", None)
        redirect_uri = session.pop("google_oauth_redirect_uri", None)
        incoming_state = (request.args.get("state") or "").strip()

        if not expected_state or not incoming_state or expected_state != incoming_state or not redirect_uri:
            raise RuntimeError("Estado OAuth inválido o expirado (state mismatch)")

        flow = _build_admin_google_flow(redirect_uri)
        if code_verifier:
            flow.code_verifier = str(code_verifier)

        flow.fetch_token(authorization_response=redirect_uri + "?" + request.query_string.decode("ascii"))
        creds = flow.credentials

        token_path = gmail_token_path()
        token_path.write_text(creds.to_json(), encoding="utf-8")
        try:
            os.chmod(token_path, 0o660)
        except Exception:
            pass

        if not admin_required():
            session["is_admin"] = True

        flash("Token de Google renovado correctamente.", "ok")
        return redirect(url_for("admin"))
    except Exception as e:
        flash(f"No se pudo completar la renovación OAuth: {e}", "error")
        return redirect(url_for("admin"))


@app.route("/executeSQL", methods=["GET", "POST"])
def execute_sql_view():
    if not admin_required():
        flash("No autorizado. Inicia sesión como admin.", "error")
        return redirect(url_for("admin"))

    cfg = load_config()
    appcfg = cfg.setdefault("app", {})
    tools_cfg = appcfg.setdefault("tools", {})
    raw_saved = tools_cfg.get("saved_sql_queries", [])
    saved_queries: List[Dict[str, str]] = []
    if isinstance(raw_saved, list):
        for item in raw_saved:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            sql = str(item.get("sql") or "").strip()
            if name and sql:
                saved_queries.append({"name": name, "sql": sql})

    sql_query_input = ""
    sql_result_cols: List[str] = []
    sql_result_rows: List[Any] = []
    sql_result_truncated = False
    sql_error = ""
    selected_query_name = ""

    def _run_query(query_text: str) -> None:
        nonlocal sql_result_cols, sql_result_rows, sql_result_truncated, sql_error
        normalized = query_text.strip().rstrip(";").strip()
        lowered = normalized.lower()
        allowed_prefixes = ("select", "show", "describe", "desc", "explain")

        if not normalized:
            sql_error = "Debes escribir una consulta SQL."
            return

        if not lowered.startswith(allowed_prefixes):
            sql_error = "Solo se permiten consultas de lectura: SELECT, SHOW, DESCRIBE, DESC o EXPLAIN."
            return

        try:
            with db().cursor() as cur:
                cur.execute(normalized)
                if cur.description:
                    sql_result_cols = [str(col[0]) for col in cur.description]
                    fetched = cur.fetchmany(201)
                    sql_result_rows = fetched[:200]
                    sql_result_truncated = len(fetched) > 200
                else:
                    sql_result_cols = ["resultado"]
                    sql_result_rows = [{"resultado": "Consulta ejecutada sin conjunto de filas."}]
        except Exception as e:
            sql_error = str(e)

    if request.method == "POST":
        action = (request.form.get("action") or "run_sql").strip().lower()
        sql_query_input = (request.form.get("sql_query") or "").strip()
        selected_query_name = (request.form.get("saved_query_name") or "").strip()

        if action == "load_query":
            loaded = next((x for x in saved_queries if x["name"] == selected_query_name), None)
            if not loaded:
                sql_error = "Consulta guardada no encontrada."
            else:
                sql_query_input = loaded["sql"]

        elif action == "save_query":
            query_name = (request.form.get("query_name") or "").strip()
            if not query_name:
                sql_error = "Debes indicar un nombre para la consulta."
            elif not sql_query_input:
                sql_error = "No hay SQL para guardar."
            else:
                replaced = False
                for item in saved_queries:
                    if item["name"] == query_name:
                        item["sql"] = sql_query_input
                        replaced = True
                        break
                if not replaced:
                    saved_queries.append({"name": query_name, "sql": sql_query_input})

                tools_cfg["saved_sql_queries"] = saved_queries
                try:
                    save_config(cfg)
                    selected_query_name = query_name
                    flash("Consulta guardada.", "ok")
                except Exception as e:
                    import logging
                    logging.exception("Error guardando config.json en executeSQL")
                    sql_error = f"Error guardando configuración: {e}"

        elif action == "delete_query":
            if not selected_query_name:
                sql_error = "Selecciona una consulta para eliminar."
            else:
                before = len(saved_queries)
                saved_queries = [x for x in saved_queries if x["name"] != selected_query_name]
                if len(saved_queries) == before:
                    sql_error = "Consulta guardada no encontrada."
                else:
                    tools_cfg["saved_sql_queries"] = saved_queries
                    save_config(cfg)
                    selected_query_name = ""
                    flash("Consulta eliminada.", "ok")

        else:
            _run_query(sql_query_input)

    return render_template(
        "execute_sql.html",
        sql_query_input=sql_query_input,
        sql_result_cols=sql_result_cols,
        sql_result_rows=sql_result_rows,
        sql_result_truncated=sql_result_truncated,
        sql_error=sql_error,
        saved_queries=saved_queries,
        selected_query_name=selected_query_name,
    )
