import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from werkzeug.exceptions import Forbidden, NotFound, ServiceUnavailable

import app as application


class NotificationCertificateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "ca.pem"
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cfg = {"app": {"notifications": {"ca_certificate_path": str(self.path)}}}

    def write_certificate(self, is_ca):
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test CA")])
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(self.key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
                .add_extension(x509.BasicConstraints(ca=is_ca, path_length=None), critical=True)
                .sign(self.key, hashes.SHA256()))
        self.path.write_bytes(cert.public_bytes(serialization.Encoding.PEM) +
                              self.key.private_bytes(serialization.Encoding.PEM,
                                                     serialization.PrivateFormat.PKCS8,
                                                     serialization.NoEncryption()))
        return cert

    def test_download_only_returns_public_certificate(self):
        cert = self.write_certificate(True)
        with application.app.test_request_context("/admin/notifications/ca-certificate"), \
             patch.object(application, "admin_required", return_value=True), \
             patch.object(application, "load_config", return_value=self.cfg):
            response = application.admin_notification_ca_certificate()
            response.direct_passthrough = False
            self.assertEqual(response.get_data(), cert.public_bytes(serialization.Encoding.PEM))
            self.assertNotIn(b"PRIVATE KEY", response.get_data())
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertIn("attachment", response.headers["Content-Disposition"])

    def test_invalid_certificates_and_missing_file_are_rejected(self):
        self.write_certificate(False)
        with self.assertRaises(ValueError):
            application.load_notification_ca_certificate(self.cfg)
        with application.app.test_request_context("/admin/notifications/ca-certificate"), \
             patch.object(application, "admin_required", return_value=True), \
             patch.object(application, "load_config", return_value=self.cfg):
            with self.assertRaises(ServiceUnavailable):
                application.admin_notification_ca_certificate()
            self.path.unlink()
            with self.assertRaises(ServiceUnavailable):
                application.admin_notification_ca_certificate()
        with self.assertRaises(ValueError):
            application.load_notification_ca_certificate(
                {"app": {"notifications": {"ca_certificate_path": "relative.pem"}}}
            )

    def test_download_requires_admin_and_configuration(self):
        with application.app.test_request_context("/admin/notifications/ca-certificate"):
            with patch.object(application, "admin_required", return_value=False), \
                 self.assertRaises(Forbidden):
                application.admin_notification_ca_certificate()
            with patch.object(application, "admin_required", return_value=True), \
                 patch.object(application, "load_config", return_value={}), \
                 self.assertRaises(NotFound):
                application.admin_notification_ca_certificate()

    def test_notification_page_versions_push_script(self):
        status = {"worker": None, "failed": [], "start_time": "08:00", "end_time": "20:00",
                  "timezone": "Europe/Madrid", "within_hours": True, "telegram_status": "ok",
                  "push_csrf": "test", "devices": [], "push_errors": []}
        with application.app.test_request_context("/admin/notifications"), \
             patch.object(application, "admin_required", return_value=True), \
             patch.object(application, "load_config", return_value={}), \
             patch.object(application, "q", return_value=[]), \
             patch.object(application, "q1", return_value=None), \
             patch.object(application, "load_folder_tree", return_value=[]), \
             patch.object(application, "load_notification_status", return_value=status):
            page = application.admin_notifications()
            self.assertIn("/static/push_settings.js?v=2", page)
            self.assertIn('id="push-test-all"', page)


if __name__ == "__main__":
    unittest.main()
