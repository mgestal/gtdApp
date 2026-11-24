import time
import unittest
from unittest.mock import patch

import app as application
import auth

PASSWORD = "una-contraseña-larga"
SECRET = auth.new_totp_secret()
AUTH_CFG = {"enabled": True, "password_hash": auth.hash_password(PASSWORD), "totp_secret": SECRET}


def current_code():
    return auth.totp_code(SECRET, int(time.time() // auth.TOTP_STEP))


class TotpTests(unittest.TestCase):
    def test_rfc6238_vector(self):
        # RFC 6238, apéndice B (SHA1): T=59 -> 94287082 (8 dígitos)
        import base64
        secret = base64.b32encode(b"12345678901234567890").decode()
        self.assertEqual(auth.totp_code(secret, 59 // 30, digits=8), "94287082")
        self.assertEqual(auth.verify_totp(secret, "287082", now=59), 1)

    def test_window_and_format(self):
        now = 1_000_000
        counter = now // 30
        self.assertEqual(auth.verify_totp(SECRET, auth.totp_code(SECRET, counter - 1), now=now), counter - 1)
        self.assertIsNone(auth.verify_totp(SECRET, auth.totp_code(SECRET, counter - 3), now=now))
        self.assertIsNone(auth.verify_totp(SECRET, "abc123", now=now))
        self.assertIsNone(auth.verify_totp(SECRET, "", now=now))

    def test_safe_next(self):
        self.assertEqual(auth.safe_next("/tasks?x=1"), "/tasks?x=1")
        for bad in ("//evil.com", "https://evil.com", "\\\\evil", None, ""):
            self.assertIsNone(auth.safe_next(bad))


class LoginFlowTests(unittest.TestCase):
    def setUp(self):
        application.app.config["TESTING"] = True
        application.app.config["SESSION_COOKIE_SECURE"] = False
        self.client = application.app.test_client()
        self.failures = 0
        self.attempts = []
        patches = [
            patch.object(auth, "get_auth_cfg", side_effect=lambda: dict(AUTH_CFG)),
            patch.object(auth, "count_failures", side_effect=lambda since, ip=None: self.failures),
            patch.object(auth, "last_success_counter", return_value=-1),
            patch.object(auth, "record_attempt", side_effect=lambda ip, ok, c=None: self.attempts.append((ip, ok, c))),
            patch.object(auth, "notify"),
            # Evita tocar la BD en el before_request general de la app
            patch.object(application, "ensure_schema_updates"),
            patch.object(application, "maybe_run_calendar_autosync"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def csrf(self):
        self.client.get("/login")
        with self.client.session_transaction() as s:
            return s["login_csrf"]

    def login(self, password=PASSWORD, code=None):
        return self.client.post("/login", data={
            "csrf_token": self.csrf(), "password": password,
            "code": code or current_code(), "next": "/search", "remember": "1"})

    def test_protected_routes(self):
        r = self.client.get("/search?q=x")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login?next=/search?q%3Dx", r.headers["Location"])
        self.assertEqual(self.client.get("/api/notifications").status_code, 401)
        self.assertEqual(self.client.post("/tasks/1/toggle").status_code, 401)

    def test_exempt_routes(self):
        self.assertEqual(self.client.get("/login").status_code, 200)
        self.assertEqual(self.client.get("/sw.js").status_code, 200)
        self.assertEqual(self.client.get("/manifest.webmanifest").status_code, 200)
        # La API de la extensión sigue con su propio token
        self.assertNotEqual(self.client.get("/api/extension/tasks").status_code, 302)

    def test_successful_login(self):
        r = self.login()
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/search"))
        self.assertTrue(self.attempts[-1][1])
        with self.client.session_transaction() as s:
            self.assertTrue(s["auth_ok"])
            self.assertTrue(s.permanent)
        with patch.object(application, "search", return_value="ok") as view:
            application.app.view_functions["search"] = view
            try:
                self.assertEqual(self.client.get("/search").status_code, 200)
            finally:
                application.app.view_functions["search"] = application.search

    def test_wrong_password_or_code(self):
        self.assertEqual(self.login(password="mala").status_code, 401)
        bad = "000000" if current_code() != "000000" else "111111"
        self.assertEqual(self.login(code=bad).status_code, 401)
        self.assertEqual([a[1] for a in self.attempts], [False, False])
        with self.client.session_transaction() as s:
            self.assertNotIn("auth_ok", s)

    def test_lockout(self):
        self.failures = auth.IP_MAX_FAILURES
        r = self.login()
        self.assertEqual(r.status_code, 429)
        self.assertEqual(self.attempts, [])

    def test_missing_csrf(self):
        r = self.client.post("/login", data={"password": PASSWORD, "code": current_code()})
        self.assertEqual(r.status_code, 400)

    def test_replayed_code_rejected(self):
        counter = int(time.time() // auth.TOTP_STEP)
        with patch.object(auth, "last_success_counter", return_value=counter + 1):
            self.assertEqual(self.login().status_code, 401)

    def test_password_change_invalidates_session(self):
        self.login()
        AUTH_CFG_OLD = dict(AUTH_CFG)
        try:
            AUTH_CFG["password_hash"] = auth.hash_password("otra-contraseña-larga")
            self.assertEqual(self.client.get("/search").status_code, 302)
        finally:
            AUTH_CFG.clear()
            AUTH_CFG.update(AUTH_CFG_OLD)

    def test_logout(self):
        self.login()
        self.client.post("/logout", data={"csrf_token": "x"})  # CSRF inválido: no cierra
        with self.client.session_transaction() as s:
            self.assertTrue(s.get("auth_ok"))
            s["logout_csrf"] = "tok"
        self.client.post("/logout", data={"csrf_token": "tok"})
        with self.client.session_transaction() as s:
            self.assertNotIn("auth_ok", s)

    def test_disabled_auth_is_open(self):
        with patch.object(auth, "get_auth_cfg", return_value={}):
            self.assertEqual(self.client.get("/login").status_code, 302)
            with patch.object(application, "search", return_value="ok") as view:
                application.app.view_functions["search"] = view
                try:
                    self.assertEqual(self.client.get("/search").status_code, 200)
                finally:
                    application.app.view_functions["search"] = application.search

    def test_enabled_without_credentials_fails_closed(self):
        with patch.object(auth, "get_auth_cfg", return_value={"enabled": True}):
            self.assertEqual(self.client.get("/search").status_code, 503)

    def test_client_ip_from_local_proxy_only(self):
        with application.app.test_request_context("/", environ_base={"REMOTE_ADDR": "127.0.0.1"},
                                                  headers={"X-Forwarded-For": "1.2.3.4, 5.6.7.8"}):
            self.assertEqual(auth.client_ip(), "5.6.7.8")
        with application.app.test_request_context("/", environ_base={"REMOTE_ADDR": "192.168.1.9"},
                                                  headers={"X-Forwarded-For": "1.2.3.4"}):
            self.assertEqual(auth.client_ip(), "192.168.1.9")


if __name__ == "__main__":
    unittest.main()
