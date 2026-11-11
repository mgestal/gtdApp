import os
import base64
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pymysql

from reminders import (local_to_utc, notification_hours, run_worker, scheduled_at,
                       within_notification_hours)
from browser_push import public_key, send_browser_push, validate_subscription


def example_subscription(endpoint="https://fcm.googleapis.com/fcm/send/test"):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    encode = lambda data: base64.urlsafe_b64encode(data).rstrip(b"=").decode()
    raw = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return {"endpoint": endpoint, "keys": {"p256dh": encode(raw),
                                          "auth": encode(b"a" * 16)}}


class BrowserPushTests(unittest.TestCase):
    def test_push_test_requests_a_repeated_notification(self):
        import app as application

        subscription = {"id": 1, "scope_path": "/gtdApp"}
        with application.app.test_request_context(
            "/admin/push/test", method="POST", json={"endpoint": "https://fcm.googleapis.com/test"}
        ), patch.object(application, "require_push_admin"), \
             patch.object(application, "load_config", return_value={}), \
             patch.object(application, "within_notification_hours", return_value=True), \
             patch.object(application, "q1", return_value=subscription), \
             patch.object(application, "send_browser_push") as send:
            response = application.admin_push_test()
            self.assertTrue(response.get_json()["ok"])
            self.assertEqual(send.call_args.args[1]["tag"], "gtd-push-test")
            self.assertIs(send.call_args.args[1]["renotify"], True)
            self.assertEqual(send.call_args.args[1]["url"], "/gtdApp/admin/notifications")

    def test_push_test_all_reports_each_result_and_expires_invalid_devices(self):
        import app as application
        from requests import Response
        from browser_push import WebPushException

        subscriptions = [
            {"id": 1, "device_name": "Uno", "scope_path": "/one"},
            {"id": 2, "device_name": "Dos", "scope_path": "/two"},
            {"id": 3, "device_name": "Tres", "scope_path": ""},
        ]
        expired = Response()
        expired.status_code = 410
        with application.app.test_request_context("/admin/push/test-all", method="POST"), \
             patch.object(application, "require_push_admin"), \
             patch.object(application, "push_test_within_hours", return_value=True), \
             patch.object(application, "q", return_value=subscriptions) as query, \
             patch.object(application, "exec_sql") as update, \
             patch.object(application, "commit") as commit, \
             patch.object(application, "send_browser_push",
                          side_effect=[None, WebPushException("expired", response=expired),
                                       OSError("failure")]) as send:
            response = application.admin_push_test_all().get_json()
            self.assertTrue(response["ok"])
            self.assertEqual([r["id"] for r in response["results"]], [1, 2, 3])
            self.assertIsNone(response["results"][0]["error"])
            self.assertIn("caducado", response["results"][1]["error"])
            self.assertIn("OSError", response["results"][2]["error"])
            self.assertIn("active=1", query.call_args.args[0])
            self.assertEqual([call.args[1]["url"] for call in send.call_args_list],
                             ["/one/admin/notifications", "/two/admin/notifications", "/admin/notifications"])
            update.assert_called_once()
            self.assertEqual(update.call_args.args[1], (2,))
            commit.assert_called_once()

    def test_push_test_all_respects_schedule_and_handles_no_devices(self):
        import app as application

        with application.app.test_request_context("/admin/push/test-all", method="POST"), \
             patch.object(application, "require_push_admin"), \
             patch.object(application, "q", return_value=[]) as query, \
             patch.object(application, "push_test_within_hours", return_value=False):
            response, status = application.admin_push_test_all()
            self.assertEqual(status, 400)
            self.assertIn("horario", response.get_json()["error"])
            query.assert_not_called()
        with application.app.test_request_context("/admin/push/test-all", method="POST"), \
             patch.object(application, "require_push_admin"), \
             patch.object(application, "q", return_value=[]), \
             patch.object(application, "push_test_within_hours", return_value=True):
            response, status = application.admin_push_test_all()
            self.assertEqual(status, 400)
            self.assertIn("No hay dispositivos", response.get_json()["error"])
        devices = [{"id": 1, "device_name": "Uno", "scope_path": ""},
                   {"id": 2, "device_name": "Dos", "scope_path": ""}]
        with application.app.test_request_context("/admin/push/test-all", method="POST"), \
             patch.object(application, "require_push_admin"), \
             patch.object(application, "q", return_value=devices), \
             patch.object(application, "push_test_within_hours", side_effect=[True, True, False]), \
             patch.object(application, "send_browser_push") as send:
            response = application.admin_push_test_all().get_json()
            self.assertIsNone(response["results"][0]["error"])
            self.assertIn("horario", response["results"][1]["error"])
            send.assert_called_once()

    def test_push_test_all_requires_admin_and_csrf(self):
        import app as application

        with patch.object(application, "ensure_schema_updates"), \
             patch.object(application, "maybe_run_calendar_autosync"), \
             patch.dict(os.environ, {"GTD_ADMIN_PASSWORD": "test"}), \
             application.app.test_client() as client:
            self.assertEqual(client.post("/admin/push/test-all").status_code, 403)
            with client.session_transaction() as session:
                session["is_admin"] = True
                session["push_csrf"] = "expected"
            self.assertEqual(client.post("/admin/push/test-all").status_code, 403)
            with patch.object(application, "push_test_within_hours", return_value=True), \
                 patch.object(application, "q", return_value=[]):
                response = client.post("/admin/push/test-all",
                                       headers={"X-GTD-Push-CSRF": "expected"})
                self.assertEqual(response.status_code, 400)
                self.assertIn("No hay dispositivos", response.get_json()["error"])

    def test_validate_subscription_rejects_non_push_endpoints_and_bad_keys(self):
        self.assertEqual(validate_subscription(example_subscription())[0],
                         "https://fcm.googleapis.com/fcm/send/test")
        for endpoint in ("http://fcm.googleapis.com/test", "https://127.0.0.1/test",
                         "https://fcm.googleapis.com.evil.example/test",
                         "https://user@fcm.googleapis.com/test"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                validate_subscription(example_subscription(endpoint))
        invalid = example_subscription()
        invalid["keys"]["auth"] = "bad"
        with self.assertRaises(ValueError):
            validate_subscription(invalid)

    def test_vapid_key_is_stable_and_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "key.pem"
            with patch("browser_push.KEY_PATH", path):
                first = public_key(create=True)
                self.assertEqual(public_key(create=True), first)
                self.assertEqual(public_key(), first)
                self.assertEqual(path.stat().st_mode & 0o777, 0o640)

    def test_real_push_encryption_and_vapid_without_network(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from requests import Response
        raw = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        encode = lambda value: base64.urlsafe_b64encode(value).rstrip(b"=").decode()
        subscription = {"endpoint": "https://fcm.googleapis.com/test", "p256dh": encode(raw),
                        "auth_key": encode(b"a" * 16), "origin": "https://example.test"}
        response = Response()
        response.status_code = 201
        with tempfile.TemporaryDirectory() as directory:
            with patch("browser_push.KEY_PATH", Path(directory) / "key.pem"):
                public_key(create=True)
                with patch("requests.Session.post", return_value=response) as post:
                    send_browser_push(subscription, {"title": "GTDApp", "body": "prueba"})
        self.assertEqual(post.call_count, 1)
        request = post.call_args.kwargs
        self.assertFalse(request["allow_redirects"])
        self.assertEqual(request["headers"]["Content-Encoding"], "aes128gcm")
        self.assertEqual(request["headers"]["ttl"], "0")
        self.assertNotIn(b"prueba", request["data"])


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tz = ZoneInfo("Europe/Madrid")

    def test_fixed_is_independent_of_due_date(self):
        value = datetime(2026, 11, 18, 8)
        self.assertEqual(scheduled_at({"kind": "fixed", "fixed_at_utc": value}, self.tz), value)

    def test_relative_uses_task_time(self):
        reminder = {"kind": "relative", "due_date": date(2026, 11, 18),
                    "due_time": timedelta(hours=17), "date_only_time": None, "offset_minutes": 30}
        self.assertEqual(scheduled_at(reminder, self.tz), datetime(2026, 11, 18, 15, 30))
        reminder["due_date"] = date(2026, 11, 19)
        self.assertEqual(scheduled_at(reminder, self.tz), datetime(2026, 11, 19, 15, 30))

    def test_date_only_requires_a_time(self):
        reminder = {"kind": "relative", "due_date": date(2026, 11, 18),
                    "due_time": None, "date_only_time": None, "offset_minutes": 1440}
        self.assertIsNone(scheduled_at(reminder, self.tz))
        reminder["date_only_time"] = time(9)
        self.assertEqual(scheduled_at(reminder, self.tz), datetime(2026, 11, 17, 8))
        reminder["due_date"] = None
        self.assertIsNone(scheduled_at(reminder, self.tz))

    def test_daylight_saving_gap_rejected_and_fold_is_first_occurrence(self):
        with self.assertRaises(ValueError):
            local_to_utc(datetime(2026, 3, 29, 2, 30), self.tz)
        self.assertEqual(local_to_utc(datetime(2026, 10, 25, 2, 30), self.tz),
                         datetime(2026, 10, 25, 0, 30))

    def test_allowed_hours_include_endpoint_and_support_overnight(self):
        start, end = notification_hours({})
        self.assertFalse(within_notification_hours(datetime(2026, 11, 18, 6, 59), self.tz, start, end))
        self.assertTrue(within_notification_hours(datetime(2026, 11, 18, 7), self.tz, start, end))
        self.assertTrue(within_notification_hours(datetime(2026, 11, 18, 19, 0, 59), self.tz, start, end))
        self.assertFalse(within_notification_hours(datetime(2026, 11, 18, 19, 1), self.tz, start, end))
        overnight = notification_hours({"notifications": {"start_time": "22:00", "end_time": "08:00"}})
        self.assertTrue(within_notification_hours(datetime(2026, 11, 18, 22), self.tz, *overnight))
        self.assertFalse(within_notification_hours(datetime(2026, 11, 18, 12), self.tz, *overnight))
        with self.assertRaises(ValueError):
            notification_hours({"notifications": {"start_time": "25:00"}})


@unittest.skipUnless(os.environ.get("GTD_REMINDER_TEST_SOCKET"), "requires an isolated MariaDB test database")
class WorkerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.conn = pymysql.connect(
            unix_socket=os.environ["GTD_REMINDER_TEST_SOCKET"], user="root",
            database="reminder_test", charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor, autocommit=False,
        )
        self.now = datetime(2026, 11, 18, 12)
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO tasks(title, due_date, due_time) VALUES('Prueba',%s,%s)",
                        (date(2026, 11, 18), time(13)))
            self.task_id = cur.lastrowid
        self.conn.commit()

    def tearDown(self):
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM tasks WHERE id=%s", (self.task_id,))
            cur.execute("DELETE FROM push_subscriptions WHERE device_name LIKE 'test-device-%%'")
        self.conn.commit()
        self.conn.close()

    def add_reminder(self, kind, when=None, offset=None):
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO task_reminders(task_id,kind,fixed_at_utc,offset_minutes,created_at_utc) "
                "VALUES(%s,%s,%s,%s,%s)",
                (self.task_id, kind, when, offset, self.now),
            )
        self.conn.commit()

    def scan(self, send):
        with patch("reminders.utc_now", return_value=self.now), patch("reminders.send_telegram", send):
            return run_worker(self.conn, ZoneInfo("Europe/Madrid"), lambda: "token", lambda: 1)

    def test_once_and_recalculated_occurrence(self):
        self.add_reminder("relative", offset=0)
        with patch("reminders.send_telegram") as send:
            self.scan(send)
            self.scan(send)
            self.assertEqual(send.call_count, 1)
            with self.conn.cursor() as cur:
                cur.execute("UPDATE tasks SET due_date=%s WHERE id=%s", (date(2026, 11, 19), self.task_id))
            self.conn.commit()
            self.now += timedelta(days=1)
            self.scan(send)
            self.assertEqual(send.call_count, 2)

    def test_failure_retry_then_sent(self):
        self.add_reminder("fixed", when=self.now - timedelta(minutes=1))
        with patch("reminders.send_telegram", side_effect=[RuntimeError("secret"), None]) as send:
            self.assertEqual(self.scan(send)["failed"], 1)
            with self.conn.cursor() as cur:
                cur.execute("SELECT status, last_error FROM reminder_deliveries")
                row = cur.fetchone()
            self.assertEqual(row["status"], "failed")
            self.assertNotIn("secret", row["last_error"])
            self.assertEqual(self.scan(send)["sent"], 1)

    def test_expired_and_completed_are_not_sent(self):
        self.add_reminder("fixed", when=self.now - timedelta(days=1))
        with patch("reminders.send_telegram") as send:
            self.assertEqual(self.scan(send)["expired"], 1)
            self.add_reminder("fixed", when=self.now - timedelta(minutes=1))
            with self.conn.cursor() as cur:
                cur.execute("UPDATE tasks SET completed_at=%s WHERE id=%s", (self.now, self.task_id))
            self.conn.commit()
            self.scan(send)
            send.assert_not_called()

    def test_archived_and_deleted_tasks_are_not_sent(self):
        self.add_reminder("fixed", when=self.now - timedelta(minutes=1))
        with patch("reminders.send_telegram") as send:
            for change in ("archived=1", "archived=0, deleted_at=NOW()"):
                with self.conn.cursor() as cur:
                    cur.execute(f"UPDATE tasks SET {change} WHERE id=%s", (self.task_id,))
                self.conn.commit()
                self.scan(send)
            send.assert_not_called()

    def test_outside_window_delays_without_marking_expired(self):
        self.now = datetime(2026, 11, 18, 19, 15)
        self.add_reminder("fixed", when=self.now - timedelta(minutes=5))
        with patch("reminders.send_telegram") as send:
            self.assertEqual(self.scan(send)["outside_window"], 1)
            send.assert_not_called()
            with self.conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM reminder_deliveries")
                self.assertEqual(cur.fetchone()["n"], 0)
            self.conn.commit()
            self.now = datetime(2026, 11, 19, 7)
            self.assertEqual(self.scan(send)["sent"], 1)
            send.assert_called_once()

    def add_push_device(self, name):
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO push_subscriptions(endpoint_hash,endpoint,p256dh,auth_key,device_name,"
                        "scope_path,origin,created_at_utc,updated_at_utc) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (name, f"https://fcm.googleapis.com/{name}", "key", "auth", name,
                         "/gtdApp", "https://example.test", self.now, self.now))
        self.conn.commit()

    def test_channels_and_device_retry_are_independent(self):
        from requests.exceptions import ConnectionError
        self.add_push_device("test-device-one")
        self.add_push_device("test-device-two")
        self.add_reminder("fixed", when=self.now - timedelta(minutes=1))
        with self.conn.cursor() as cur:
            cur.execute("UPDATE task_reminders SET send_browser=1 WHERE task_id=%s", (self.task_id,))
        self.conn.commit()
        with patch("reminders.send_telegram", side_effect=[RuntimeError("telegram"), None]) as telegram:
            with patch("reminders.send_browser_push", side_effect=[None, ConnectionError("push"), None]) as push:
                first = self.scan(telegram)
                self.assertEqual(first["failed"], 2)
                self.assertEqual(first["sent"], 1)
                second = self.scan(telegram)
                self.assertEqual(second["sent"], 2)
                self.scan(telegram)
                self.assertEqual(push.call_count, 3)
                self.assertEqual(telegram.call_count, 2)
                self.assertEqual(push.call_args.args[1]["url"], f"/gtdApp/tasks/{self.task_id}/edit")

    def test_push_only_never_calls_telegram_and_respects_window(self):
        self.add_push_device("test-device-one")
        self.add_reminder("fixed", when=self.now - timedelta(minutes=1))
        with self.conn.cursor() as cur:
            cur.execute("UPDATE task_reminders SET send_telegram=0,send_browser=1 WHERE task_id=%s", (self.task_id,))
        self.conn.commit()
        with patch("reminders.send_telegram") as telegram, patch("reminders.send_browser_push") as push:
            self.now = datetime(2026, 11, 18, 19, 15)
            self.assertEqual(self.scan(telegram)["outside_window"], 1)
            push.assert_not_called()
            self.now = datetime(2026, 11, 19, 7)
            self.scan(telegram)
            push.assert_called_once()
            telegram.assert_not_called()

    def test_no_devices_retries_after_registration(self):
        self.add_reminder("fixed", when=self.now - timedelta(minutes=1))
        with self.conn.cursor() as cur:
            cur.execute("UPDATE task_reminders SET send_telegram=0,send_browser=1 WHERE task_id=%s", (self.task_id,))
        self.conn.commit()
        with patch("reminders.send_telegram") as telegram, patch("reminders.send_browser_push") as push:
            self.scan(telegram)
            push.assert_not_called()
            self.add_push_device("test-device-one")
            self.scan(telegram)
            self.scan(telegram)
            push.assert_called_once()

    def test_invalid_subscription_is_marked_and_not_retried(self):
        from requests import Response
        from pywebpush import WebPushException
        self.add_push_device("test-device-one")
        self.add_reminder("fixed", when=self.now - timedelta(minutes=1))
        with self.conn.cursor() as cur:
            cur.execute("UPDATE task_reminders SET send_telegram=0,send_browser=1 WHERE task_id=%s", (self.task_id,))
        self.conn.commit()
        response = Response()
        response.status_code = 410
        with patch("reminders.send_telegram") as telegram, patch("reminders.send_browser_push",
                side_effect=WebPushException("expired", response=response)) as push:
            self.scan(telegram)
            self.scan(telegram)
            push.assert_called_once()
        with self.conn.cursor() as cur:
            cur.execute("SELECT active FROM push_subscriptions WHERE device_name='test-device-one'")
            self.assertEqual(cur.fetchone()["active"], 0)


@unittest.skipUnless(os.environ.get("GTD_REMINDER_TEST_SOCKET"), "requires an isolated MariaDB test database")
class WebIntegrationTests(unittest.TestCase):
    def setUp(self):
        import copy
        import app as application

        self.application = application
        config = copy.deepcopy(application.DEFAULT_CONFIG)
        config.setdefault("app", {})["timezone"] = "Europe/Madrid"
        socket = os.environ["GTD_REMINDER_TEST_SOCKET"]

        def connect():
            return pymysql.connect(unix_socket=socket, user="root", database="reminder_test",
                                   charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor)

        self.connect = connect
        self.patches = [
            patch.object(application, "get_db_conn", side_effect=connect),
            patch.object(application, "load_config", return_value=config),
            patch.object(application, "maybe_run_calendar_autosync"),
            patch.object(application, "gmail_token_path", return_value=application.BASE_DIR / "missing-test-token"),
            patch.dict(os.environ, {"GTD_ADMIN_PASSWORD": "testing"}),
        ]
        for item in self.patches:
            item.start()
        with application.app.app_context():
            application._schema_bootstrapped = False
            application.ensure_schema_updates()
            application.db().close()
        self.conn = connect()
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO tasks(title, due_date) VALUES('Tarea de prueba', '2099-01-03')")
            self.task_id = cur.lastrowid
        self.conn.commit()
        self.client = application.app.test_client()

    def tearDown(self):
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM tasks WHERE id=%s", (self.task_id,))
        self.conn.commit()
        self.conn.close()
        for item in reversed(self.patches):
            item.stop()

    def test_add_display_badge_delete_and_admin(self):
        path = f"/tasks/{self.task_id}/reminders"
        response = self.client.post(path, data={"kind": "relative", "amount": "1", "unit": "days",
                                               "date_only_time": "09:00", "next": "/", "send_telegram": "1"})
        self.assertEqual(response.status_code, 302)
        with self.conn.cursor() as cur:
            cur.execute("SELECT id FROM task_reminders WHERE task_id=%s", (self.task_id,))
            reminder_id = cur.fetchone()["id"]
        response = self.client.get(f"/tasks/{self.task_id}/edit")
        self.assertEqual(response.status_code, 200)
        self.assertIn("02-01-2099 09:00".encode(), response.data)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'class="reminder-badge"', response.data)
        with self.client.session_transaction() as session:
            session["is_admin"] = True
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Abrir panel de notificaciones".encode(), response.data)
        response = self.client.get("/admin/notifications")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Dispositivos suscritos".encode(), response.data)
        self.client.post(f"{path}/{reminder_id}/delete", data={"next": "/"})
        self.conn.commit()
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM task_reminders WHERE task_id=%s", (self.task_id,))
            self.assertEqual(cur.fetchone()["n"], 0)

    def test_relative_requires_date_only_time(self):
        response = self.client.post(f"/tasks/{self.task_id}/reminders",
                                    data={"kind": "relative", "amount": "1", "unit": "days"},
                                    follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Indica la hora de referencia".encode(), response.data)
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM task_reminders WHERE task_id=%s", (self.task_id,))
            self.assertEqual(cur.fetchone()["n"], 0)

    def test_preview_edit_and_recalculate_after_task_date_change(self):
        path = f"/tasks/{self.task_id}/reminders"
        data = {"kind": "relative", "amount": "1", "unit": "days", "date_only_time": "09:00",
                "send_telegram": "1", "send_browser": "1"}
        response = self.client.post(f"{path}/preview", data=data)
        self.assertEqual(response.status_code, 200)
        self.assertIn("02-01-2099 a las 09:00", response.get_json()["message"])
        self.client.post(path, data=data)
        with self.conn.cursor() as cur:
            cur.execute("SELECT id FROM task_reminders WHERE task_id=%s", (self.task_id,))
            reminder_id = cur.fetchone()["id"]
        response = self.client.post(f"{path}/{reminder_id}/edit",
                                    data={"kind": "relative", "amount": "2", "unit": "days",
                                          "date_only_time": "09:00", "send_browser": "1"})
        self.assertEqual(response.status_code, 302)
        response = self.client.get(f"/tasks/{self.task_id}/edit")
        self.assertIn(b"01-01-2099 09:00", response.data)
        with self.conn.cursor() as cur:
            cur.execute("UPDATE tasks SET due_date='2099-01-05' WHERE id=%s", (self.task_id,))
        self.conn.commit()
        response = self.client.get(f"/tasks/{self.task_id}/edit")
        self.assertIn(b"03-01-2099 09:00", response.data)

    def test_admin_can_save_notification_hours(self):
        with self.client.session_transaction() as session:
            session["is_admin"] = True
        with patch.object(self.application, "save_config") as save:
            response = self.client.post("/admin", data={"action": "save_reminder_hours",
                                                       "start_time": "22:00", "end_time": "08:00"})
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.location.endswith("/admin/notifications"))
            settings = save.call_args.args[0]["app"]["notifications"]
            self.assertEqual(settings, {"start_time": "22:00", "end_time": "08:00"})

    def test_notification_panel_requires_admin(self):
        response = self.client.get("/admin/notifications")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/admin"))

    def test_push_admin_subscription_and_csrf(self):
        subscription = example_subscription()
        payload = {"subscription": subscription, "name": "Mi navegador", "origin": "https://localhost"}
        self.assertEqual(self.client.post("/admin/push/subscribe", json=payload).status_code, 403)
        with self.client.session_transaction() as session:
            session["is_admin"] = True
            session["push_csrf"] = "csrf-test"
        self.assertEqual(self.client.post("/admin/push/subscribe", json=payload).status_code, 403)
        headers = {"X-GTD-Push-CSRF": "csrf-test"}
        with patch.object(self.application, "public_key", return_value="key"):
            result = self.client.post("/admin/push/subscribe", json=payload, headers=headers,
                                      base_url="https://localhost")
        self.assertEqual(result.status_code, 200)
        with self.conn.cursor() as cur:
            cur.execute("SELECT id, device_name, scope_path FROM push_subscriptions WHERE device_name='Mi navegador'")
            row = cur.fetchone()
        self.assertEqual(row["scope_path"], "")
        self.assertEqual(self.client.post("/admin/push/status", json={"endpoint": subscription["endpoint"]},
                                          headers=headers).get_json()["registered"], True)
        self.assertEqual(self.client.post(f"/admin/push/{row['id']}/delete", json={}, headers=headers).status_code, 200)

    def test_reminder_requires_at_least_one_channel(self):
        response = self.client.post(f"/tasks/{self.task_id}/reminders",
                                    data={"kind": "fixed", "fixed_at": "2099-01-03T09:00"},
                                    follow_redirects=True)
        self.assertIn("Selecciona al menos un canal".encode(), response.data)


if __name__ == "__main__":
    unittest.main()
