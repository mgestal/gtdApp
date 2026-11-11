import unittest
import os
import uuid
from datetime import datetime
from unittest.mock import patch

import app as application
import pymysql


class NotificationCenterTests(unittest.TestCase):
    def test_group_channels_and_devices_by_occurrence(self):
        when = datetime(2026, 11, 11, 12, 0)
        common = {"reminder_id": 7, "task_id": 2, "title": "Tarea",
                  "scheduled_at_utc": when, "updated_at_utc": when, "read_at_utc": None}
        rows = [{**common, "channel": "Telegram", "device": None},
                {**common, "channel": "Navegador", "device": "Móvil"},
                {**common, "channel": "Navegador", "device": "Portátil"},
                {**common, "scheduled_at_utc": datetime(2026, 11, 10, 12),
                 "read_at_utc": when, "channel": "Telegram", "device": None}]
        cfg = {"app": {"timezone": "Europe/Madrid", "notifications": {"history_days": 1}}}
        with application.app.test_request_context("/api/notifications", environ_overrides={"SCRIPT_NAME": "/gtd"}), \
             patch.object(application, "load_config", return_value=cfg), \
             patch.object(application, "utc_now", return_value=datetime(2026, 11, 12, 12)), \
             patch.object(application, "q", return_value=rows) as query:
            response = application.notification_history()
        items = response.get_json()["notifications"]
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["channels"], ["Telegram", "Navegador: Móvil", "Navegador: Portátil"])
        self.assertEqual(items[0]["url"], "/gtd/tasks/2/edit")
        self.assertFalse(items[0]["read"])
        self.assertTrue(items[1]["read"])
        self.assertEqual(query.call_args.args[1], (when, when))
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_history_days_validation(self):
        self.assertEqual(application.notification_history_days({}), 1)
        for value in (0, 366, "1", True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                application.notification_history_days({"app": {"notifications": {"history_days": value}}})

    def test_mark_read_requires_csrf_valid_data_and_sent_delivery(self):
        payload = {"reminder_id": 2, "scheduled_at": "2026-11-11T12:00:00"}
        with application.app.test_request_context("/api/notifications/read", method="POST", json=payload):
            with self.assertRaises(application.app.aborter.mapping[403]):
                application.notification_mark_read()
        headers = {"X-GTD-Notification-CSRF": "token"}
        for value in ("bad", "2026-11-11T12:00:00+00:00"):
            with application.app.test_request_context("/api/notifications/read", method="POST",
                    headers=headers, json={**payload, "scheduled_at": value}):
                application.session["notification_csrf"] = "token"
                response, status = application.notification_mark_read()
                self.assertEqual(status, 400)
        with application.app.test_request_context("/api/notifications/read", method="POST", headers=headers, json=payload), \
             patch.object(application, "q1", return_value=None):
            application.session["notification_csrf"] = "token"
            response, status = application.notification_mark_read()
            self.assertEqual(status, 404)
        with application.app.test_request_context("/api/notifications/read", method="POST", headers=headers, json=payload), \
             patch.object(application, "q1", return_value={"sent": 1}), \
             patch.object(application, "exec_sql") as execute, \
             patch.object(application, "commit") as commit:
            application.session["notification_csrf"] = "token"
            self.assertTrue(application.notification_mark_read().get_json()["ok"])
            self.assertEqual(execute.call_args.args[1][:2], (2, datetime(2026, 11, 11, 12)))
            commit.assert_called_once()

    def test_admin_history_setting_rejects_bad_values(self):
        for raw in ("0", "366", "1.5", ""):
            with self.subTest(raw=raw), application.app.test_request_context("/admin", method="POST", data={
                    "action": "save_notification_history", "history_days": raw}), \
                 patch.object(application, "admin_required", return_value=True), \
                 patch.object(application, "save_config") as save:
                application.admin()
                save.assert_not_called()
        cfg = {"app": {"notifications": {}}}
        with application.app.test_request_context("/admin", method="POST", data={
                "action": "save_notification_history", "history_days": "12"}), \
             patch.object(application, "admin_required", return_value=True), \
             patch.object(application, "load_config", return_value=cfg), \
             patch.object(application, "save_config") as save:
            application.admin()
            self.assertEqual(cfg["app"]["notifications"]["history_days"], 12)
            save.assert_called_once_with(cfg)


@unittest.skipUnless(os.environ.get("GTD_REMINDER_TEST_SOCKET"), "requires an isolated MariaDB test database")
class NotificationCenterDatabaseTests(unittest.TestCase):
    def setUp(self):
        socket = os.environ["GTD_REMINDER_TEST_SOCKET"]

        def connect():
            return pymysql.connect(unix_socket=socket, user="root", database="reminder_test",
                                   charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor)

        self.connect = connect
        self.patches = [
            patch.object(application, "get_db_conn", side_effect=connect),
            patch.object(application, "load_config", return_value={"app": {"timezone": "Europe/Madrid"}}),
            patch.object(application, "maybe_run_calendar_autosync"),
        ]
        for item in self.patches:
            item.start()
        application._schema_bootstrapped = False
        with application.app.app_context():
            application.ensure_schema_updates()
            application.db().close()
        self.conn = connect()
        self.now = application.utc_now().replace(microsecond=0)
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO tasks(title) VALUES('Prueba campana')")
            self.task_id = cur.lastrowid
            cur.execute("INSERT INTO tags(name) VALUES(%s)", ("location-test-" + uuid.uuid4().hex,))
            self.tag_id = cur.lastrowid
            cur.execute("INSERT INTO task_tags(task_id,tag_id) VALUES(%s,%s)", (self.task_id, self.tag_id))
            cur.execute("INSERT INTO tag_locations(tag_id,latitude,longitude,radius_m) VALUES(%s,42,-8,200)", (self.tag_id,))
            cur.execute("INSERT INTO task_reminders(task_id,kind,created_at_utc) VALUES(%s,'fixed',%s)",
                        (self.task_id, self.now))
            self.reminder_id = cur.lastrowid
            cur.execute("INSERT INTO reminder_deliveries(reminder_id,scheduled_at_utc,status,updated_at_utc) "
                        "VALUES(%s,%s,'sent',%s)", (self.reminder_id, self.now, self.now))
            cur.execute("INSERT INTO push_subscriptions(endpoint_hash,endpoint,p256dh,auth_key,device_name,"
                        "scope_path,origin,created_at_utc,updated_at_utc) VALUES(%s,'test','test','test',"
                        "'Móvil','/','https://example.test',%s,%s)", (uuid.uuid4().hex, self.now, self.now))
            self.subscription_id = cur.lastrowid
            cur.execute("INSERT INTO push_occurrences(reminder_id,scheduled_at_utc,status) VALUES(%s,%s,'initialized')",
                        (self.reminder_id, self.now))
            occurrence_id = cur.lastrowid
            cur.execute("INSERT INTO push_deliveries(occurrence_id,subscription_id,status,updated_at_utc) "
                        "VALUES(%s,%s,'sent',%s)", (occurrence_id, self.subscription_id, self.now))
        self.conn.commit()

    def tearDown(self):
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM tasks WHERE id=%s", (self.task_id,))
            cur.execute("DELETE FROM tags WHERE id=%s", (self.tag_id,))
            cur.execute("DELETE FROM push_subscriptions WHERE id=%s", (self.subscription_id,))
        self.conn.commit()
        self.conn.close()
        for item in reversed(self.patches):
            item.stop()

    def test_grouping_and_shared_read_state_with_real_database(self):
        first = application.app.test_client()
        second = application.app.test_client()
        with first.session_transaction() as session:
            session["notification_csrf"] = "test-token"
        with patch.object(application, "push_test_within_hours", return_value=True):
            locations = first.get("/api/location-alerts").get_json()["locations"]
        self.assertIn(self.task_id, next(item for item in locations if item["id"] == self.tag_id)["task_ids"])
        items = first.get("/api/notifications").get_json()["notifications"]
        item = next(item for item in items if item["reminder_id"] == self.reminder_id)
        self.assertCountEqual(item["channels"], ["Telegram", "Navegador: Móvil"])
        self.assertFalse(item["read"])
        response = first.post("/api/notifications/read", headers={"X-GTD-Notification-CSRF": "test-token"},
                              json={"reminder_id": self.reminder_id, "scheduled_at": item["scheduled_at"]})
        self.assertEqual(response.status_code, 200)
        items = second.get("/api/notifications").get_json()["notifications"]
        self.assertTrue(next(item for item in items if item["reminder_id"] == self.reminder_id)["read"])


if __name__ == "__main__":
    unittest.main()
