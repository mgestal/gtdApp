"""Integración con Gmail: rutas de credenciales, OAuth de admin y control de correos importados."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from google_auth_oauthlib.flow import InstalledAppFlow

from gtd.config import BASE_DIR
from gtd.db import commit, exec_sql, q1


def gmail_credentials_path() -> Path:
    return BASE_DIR / "instance" / "gmail_credentials.json"


def gmail_token_path() -> Path:
    return BASE_DIR / "instance" / "gmail_token.json"


def gmail_default_query() -> str:
    """
    Query por defecto para importar.
    Recomendación: usar una etiqueta específica, por ejemplo:
    label:ToGTD in:inbox
    """
    return os.environ.get("GTD_GMAIL_QUERY", "label:ToGTD in:inbox")


GOOGLE_OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar",
]


def _build_admin_google_flow(redirect_uri: str) -> InstalledAppFlow:
    return InstalledAppFlow.from_client_secrets_file(
        str(gmail_credentials_path()),
        GOOGLE_OAUTH_SCOPES,
        redirect_uri=redirect_uri,
    )


def _is_google_invalid_grant_error(err: Any) -> bool:
    txt = (str(err) or "").lower()
    return (
        "invalid_grant" in txt
        or "token has been expired or revoked" in txt
        or "token has expired or revoked" in txt
    )


def ensure_imported_emails_table() -> None:
    exec_sql(
        "CREATE TABLE IF NOT EXISTS imported_emails ("
        "id INT AUTO_INCREMENT PRIMARY KEY, "
        "gmail_message_id VARCHAR(255) NOT NULL UNIQUE, "
        "gmail_thread_id VARCHAR(255) NULL, "
        "task_id INT NOT NULL, "
        "imported_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
        "INDEX idx_imported_emails_task_id (task_id)"
        ")"
    )
    commit()


def gmail_message_already_imported(message_id: str) -> bool:
    row = q1(
        "SELECT id FROM imported_emails WHERE gmail_message_id=%s",
        (message_id,),
    )
    return row is not None
