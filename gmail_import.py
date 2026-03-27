from __future__ import annotations

import base64
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


# Solo lectura. Si más adelante quieres modificar etiquetas en Gmail,
# cambia a gmail.modify.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar",
]


def build_gmail_service(
    credentials_path: str | Path,
    token_path: str | Path,
):
    credentials_path = Path(credentials_path)
    token_path = Path(token_path)

    creds: Optional[Credentials] = None

    print("[1] Comprobando token existente...")
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.valid:
        print("[2] Token válido encontrado.")
        return build("gmail", "v1", credentials=creds)

    if creds and creds.expired and creds.refresh_token:
        print("[2] Refrescando token expirado...")
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")
        print("[3] Token refrescado.")
        return build("gmail", "v1", credentials=creds)

    print("[2] No hay token válido. Iniciando flujo manual OAuth...")
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)

    # Redirect fijo para que el flujo sea consistente
    flow.redirect_uri = "http://localhost:8080/"

    auth_url, _state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )

    print("\n" + "=" * 80)
    print("AUTORIZACIÓN MANUAL DE GMAIL")
    print("=" * 80)
    print("1) Abre esta URL en tu navegador:\n")
    print(auth_url)
    print("\n2) Inicia sesión con la cuenta de Gmail que quieres importar.")
    print("3) Acepta los permisos.")
    print("4) Cuando el navegador vaya a http://localhost:8080/... y falle,")
    print("   copia la URL COMPLETA de la barra del navegador y pégala aquí.\n")

    authorization_response = input("Pega la URL completa de retorno: ").strip()

    print("[3] Intercambiando código por token...")
    flow.fetch_token(authorization_response=authorization_response)

    creds = flow.credentials
    print("[4] Token recibido. Guardando en disco...")
    token_path.write_text(creds.to_json(), encoding="utf-8")

    print("[5] Construyendo cliente Gmail API...")
    return build("gmail", "v1", credentials=creds)


def list_matching_messages(service, gmail_query: str, max_results: int = 25) -> List[Dict[str, Any]]:
    """
    Devuelve una lista de mensajes con id y threadId.
    """
    resp = (
        service.users()
        .messages()
        .list(userId="me", q=gmail_query, maxResults=max_results)
        .execute()
    )
    return resp.get("messages", [])


def get_message_metadata(service, message_id: str) -> Dict[str, Any]:
    """
    Recupera metadatos útiles del mensaje.
    """
    return (
        service.users()
        .messages()
        .get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["Subject", "From", "Date"],
        )
        .execute()
    )


def header_value(msg: Dict[str, Any], name: str) -> str:
    headers = msg.get("payload", {}).get("headers", []) or []
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "") or ""
    return ""


def parse_gmail_date(date_header: str) -> Optional[datetime]:
    if not date_header:
        return None
    try:
        return parsedate_to_datetime(date_header)
    except Exception:
        return None


def gmail_message_url(message_id: str) -> str:
    # Enlace simple y estable al mensaje en Gmail web
    return f"https://mail.google.com/mail/u/0/#all/{message_id}"


def snippet_to_notes(
    subject: str,
    from_value: str,
    date_value: str,
    snippet: str,
    message_id: str,
) -> str:
    parts = [
        f"Asunto: {subject or '(sin asunto)'}",
        f"Remitente: {from_value or '(desconocido)'}",
        f"Fecha: {date_value or '(sin fecha)'}",
        f"Enlace Gmail: {gmail_message_url(message_id)}",
        "",
        "Snippet:",
        snippet or "",
    ]
    return "\n".join(parts).strip()


def message_to_task_payload(msg: Dict[str, Any]) -> Dict[str, Any]:
    subject = header_value(msg, "Subject").strip() or "(sin asunto)"
    from_value = header_value(msg, "From").strip()
    date_value = header_value(msg, "Date").strip()
    snippet = (msg.get("snippet") or "").strip()
    message_id = msg.get("id", "")
    thread_id = msg.get("threadId", "")

    parsed_dt = parse_gmail_date(date_value)
    due_date = None  # No asignamos fecha automáticamente

    title = f"{subject}"[:255]
    notes = snippet_to_notes(
        subject=subject,
        from_value=from_value,
        date_value=date_value,
        snippet=snippet,
        message_id=message_id,
    )

    return {
        "gmail_message_id": message_id,
        "gmail_thread_id": thread_id,
        "title": title,
        "notes": notes,
        "due_date": due_date,
    }
