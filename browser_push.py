from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException, webpush
import requests


KEY_PATH = Path(__file__).resolve().parent / "instance" / ".vapid-private.pem"

PUSH_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS push_subscriptions (
        id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        endpoint_hash CHAR(64) NOT NULL,
        endpoint TEXT NOT NULL,
        p256dh VARCHAR(128) NOT NULL,
        auth_key VARCHAR(128) NOT NULL,
        device_name VARCHAR(100) NOT NULL,
        scope_path VARCHAR(255) NOT NULL,
        origin VARCHAR(255) NOT NULL,
        active TINYINT(1) NOT NULL DEFAULT 1,
        created_at_utc DATETIME NOT NULL,
        updated_at_utc DATETIME NOT NULL,
        UNIQUE KEY uq_push_endpoint (endpoint_hash)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS push_occurrences (
        id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        reminder_id INT NOT NULL,
        scheduled_at_utc DATETIME NOT NULL,
        status VARCHAR(16) NOT NULL,
        UNIQUE KEY uq_push_occurrence (reminder_id, scheduled_at_utc),
        CONSTRAINT fk_push_occ_reminder FOREIGN KEY (reminder_id)
            REFERENCES task_reminders(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS push_deliveries (
        id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
        occurrence_id INT NOT NULL,
        subscription_id INT NOT NULL,
        status VARCHAR(16) NOT NULL,
        attempts INT NOT NULL DEFAULT 0,
        updated_at_utc DATETIME NOT NULL,
        last_error VARCHAR(255) DEFAULT NULL,
        UNIQUE KEY uq_push_delivery (occurrence_id, subscription_id),
        CONSTRAINT fk_push_delivery_occ FOREIGN KEY (occurrence_id)
            REFERENCES push_occurrences(id) ON DELETE CASCADE,
        CONSTRAINT fk_push_delivery_sub FOREIGN KEY (subscription_id)
            REFERENCES push_subscriptions(id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
)


def public_key(create: bool = False) -> str:
    if create and not KEY_PATH.exists():
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        fd, tmp_name = tempfile.mkstemp(prefix=".vapid-", dir=KEY_PATH.parent)
        try:
            os.fchmod(fd, 0o640)
            with os.fdopen(fd, "wb") as file:
                file.write(pem)
            try:
                os.link(tmp_name, KEY_PATH)
            except FileExistsError:
                pass
        finally:
            os.unlink(tmp_name)
    private = serialization.load_pem_private_key(KEY_PATH.read_bytes(), password=None)
    if not isinstance(private, ec.EllipticCurvePrivateKey) or not isinstance(private.curve, ec.SECP256R1):
        raise ValueError("La clave VAPID no es válida.")
    raw = private.public_key().public_bytes(serialization.Encoding.X962,
                                             serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def validate_subscription(data: object) -> tuple[str, str, str]:
    if not isinstance(data, dict) or not isinstance(data.get("keys"), dict):
        raise ValueError("Suscripción incompleta.")
    endpoint = data.get("endpoint")
    keys = data["keys"]
    if not isinstance(endpoint, str) or len(endpoint) > 2048:
        raise ValueError("Endpoint de suscripción no válido.")
    parsed = urlparse(endpoint)
    host = (parsed.hostname or "").lower()
    allowed = (host == "fcm.googleapis.com" or host == "updates.push.services.mozilla.com"
               or host.endswith(".push.apple.com") or host.endswith(".notify.windows.com"))
    if (parsed.scheme != "https" or not allowed or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.fragment):
        raise ValueError("Proveedor de notificaciones no admitido.")
    result = []
    for field, length in (("p256dh", 65), ("auth", 16)):
        value = keys.get(field)
        if not isinstance(value, str) or len(value) > 128:
            raise ValueError("Claves de suscripción no válidas.")
        try:
            raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("Claves de suscripción no válidas.") from exc
        if len(raw) != length or (field == "p256dh" and raw[0] != 4):
            raise ValueError("Claves de suscripción no válidas.")
        if field == "p256dh":
            try:
                ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
            except ValueError as exc:
                raise ValueError("Clave pública de suscripción no válida.") from exc
        result.append(value)
    return endpoint, result[0], result[1]


def endpoint_hash(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


def send_browser_push(subscription: dict, payload: dict) -> None:
    class NoRedirectSession(requests.Session):
        def post(self, url, **kwargs):
            return super().post(url, allow_redirects=False, **kwargs)

    with NoRedirectSession() as session:
        webpush(
            subscription_info={"endpoint": subscription["endpoint"],
                               "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth_key"]}},
            data=json.dumps(payload, ensure_ascii=False),
            vapid_private_key=str(KEY_PATH),
            vapid_claims={"sub": subscription["origin"]},
            ttl=0,
            timeout=15,
            requests_session=session,
        )
