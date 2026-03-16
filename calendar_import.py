from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from datetime import datetime, date, timedelta, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build


SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
]


def build_google_service(
    credentials_path: str | Path,
    token_path: str | Path,
    api_name: str,
    api_version: str,
):
    credentials_path = Path(credentials_path)
    token_path = Path(token_path)

    creds: Optional[Credentials] = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.valid:
        return build(api_name, api_version, credentials=creds)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")
        return build(api_name, api_version, credentials=creds)

    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)

    # flujo manual
    flow.redirect_uri = "http://localhost:8080/"

    auth_url, _state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )

    print("\n" + "=" * 80)
    print("AUTORIZACIÓN MANUAL GOOGLE")
    print("=" * 80)
    print("1) Abre esta URL en tu navegador:\n")
    print(auth_url)
    print("\n2) Inicia sesión con la cuenta correcta.")
    print("3) Cuando el navegador vaya a http://localhost:8080/... y falle,")
    print("   copia la URL COMPLETA de la barra del navegador y pégala aquí.\n")

    authorization_response = input("Pega la URL completa de retorno: ").strip()

    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
    flow.fetch_token(authorization_response=authorization_response)

    creds = flow.credentials
    token_path.write_text(creds.to_json(), encoding="utf-8")

    return build(api_name, api_version, credentials=creds)


def list_upcoming_events(
    service,
    calendar_id: str = "mgestal@gmail.com",
    days_range: str = "today",
):
    """
    Devuelve eventos en función de la fecha del evento.

    days_range:
      - "today"
      - "7days"
      - "15days"
    """
    now = datetime.now(timezone.utc)

    if days_range == "today":
        time_min = now.replace(hour=0, minute=0, second=0, microsecond=0)
        time_max = time_min + timedelta(days=1)
    elif days_range == "15days":
        time_min = now
        time_max = now + timedelta(days=15)
    else:
        # por defecto: 7 días
        time_min = now
        time_max = now + timedelta(days=7)

    result = (
        service.events()
        .list(
            calendarId=calendar_id,
            timeMin=time_min.isoformat(),
            timeMax=time_max.isoformat(),
            maxResults=250,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )

    return result.get("items", [])



def event_to_task_payload(ev: Dict[str, Any]) -> Dict[str, Any]:
    title = (ev.get("summary") or "(sin título)").strip()

    start = ev.get("start", {})
    start_date = start.get("date") or start.get("dateTime")

    due_date = None
    if start_date:
        if "T" in start_date:
            due_date = datetime.fromisoformat(start_date.replace("Z", "+00:00")).date()
        else:
            due_date = datetime.strptime(start_date, "%Y-%m-%d").date()

    location = (ev.get("location") or "").strip()
    description = (ev.get("description") or "").strip()
    html_link = (ev.get("htmlLink") or "").strip()

    notes_parts = []
    if location:
        notes_parts.append(f"Ubicación: {location}")
    if description:
        notes_parts.append(f"Descripción: {description}")
    if html_link:
        notes_parts.append(f"Enlace: {html_link}")

    notes = "\n\n".join(notes_parts) if notes_parts else None

    return {
        "google_event_id": ev.get("id"),
        "title": title,
        "due_date": due_date,
        "notes": notes,
    }

def list_recent_events_by_created(
    service,
    calendar_id: str = "mgestal@gmail.com",
    created_range: str = "today",
):
    """
    Devuelve eventos filtrados por fecha de creación.

    created_range:
      - "today"
      - "7days"
      - "15days"
    """
    now = datetime.now(timezone.utc)

    # Ventana razonable de búsqueda de eventos
    time_min = (now - timedelta(days=30)).isoformat()
    time_max = (now + timedelta(days=365)).isoformat()

    result = (
        service.events()
        .list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            maxResults=250,
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
    )

    events = result.get("items", [])

    if created_range == "today":
        threshold = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif created_range == "15days":
        threshold = now - timedelta(days=15)
    else:
        threshold = now - timedelta(days=7)

    filtered = []

    for ev in events:
        created_raw = ev.get("created")
        if not created_raw:
            continue

        try:
            created_dt = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
        except Exception:
            continue

        if created_dt >= threshold:
            filtered.append(ev)

    filtered.sort(
        key=lambda ev: ev.get("created", ""),
        reverse=True,
    )

    return filtered