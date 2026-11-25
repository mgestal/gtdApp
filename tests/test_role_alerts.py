"""Avisos automáticos por etiqueta GTD: EnSeguimiento y Agenda (gtd/role_alerts.py)."""
import os
import unittest
from datetime import date, datetime, time, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from flask import session

import app
import reminders
from gtd import role_alerts as ra
from gtd.views import admin as admin_views
from gtd.views import notifications as notifications_views
from tests.support import patch_all

TZ = ZoneInfo("Europe/Madrid")
# 25-11-2026 10:00 en Madrid (UTC+1)
NOW = datetime(2026, 11, 25, 9, 0)
SCHEDULED = datetime(2026, 11, 25, 8, 0)  # 09:00 en Madrid
ALL_ON = {"enabled": True, "time": "09:00", "telegram": True, "browser": True, "app": True}
CFG = {"app": {"notifications": {"following": ALL_ON, "agenda": {"enabled": False}}}}


class SettingsTests(unittest.TestCase):
    def test_defaults_following_on_agenda_off(self):
        settings = ra.all_role_alert_settings({})
        self.assertEqual(settings["following"], {"enabled": True, "time": time(9, 0), "telegram": True,
                                                 "browser": True, "app": True})
        self.assertFalse(settings["agenda"]["enabled"])

    def test_overrides_and_invalid_time(self):
        cfg = {"app": {"notifications": {"agenda": {"enabled": True, "time": "25:99", "browser": False, "app": False}}}}
        s = ra.role_alert_settings("agenda", cfg)
        self.assertEqual((s["enabled"], s["time"], s["telegram"], s["browser"], s["app"]),
                         (True, time(9, 0), True, False, False))

    def test_scheduled_for_default_time_or_10_minutes_before_task_time(self):
        self.assertEqual(ra.scheduled_for(date(2026, 11, 25), None, time(9, 0), TZ), SCHEDULED)
        # tarea con hora: 10 min antes, adelantado al tramo de 15 min anterior (pasada del proceso)
        cases = [
            (timedelta(hours=7, minutes=30), datetime(2026, 11, 25, 6, 15)),   # 07:20 -> 07:15 Madrid
            (timedelta(hours=7, minutes=40), datetime(2026, 11, 25, 6, 30)),   # 07:30 -> 07:30
            (timedelta(hours=7, minutes=24, seconds=30), datetime(2026, 11, 25, 6, 0)),  # 07:14:30 -> 07:00
            (timedelta(hours=7, minutes=55), datetime(2026, 11, 25, 6, 45)),   # 07:45 -> 07:45
        ]
        for due_time, expected in cases:
            with self.subTest(due_time=due_time):
                at = ra.scheduled_for(date(2026, 11, 25), due_time, time(9, 0), TZ)
                self.assertEqual(at, expected)
                self.assertGreaterEqual(datetime(2026, 11, 25) + due_time - timedelta(hours=1) - at, timedelta(minutes=10))
        # tarea a las 00:05 -> aviso el día anterior a las 23:45
        self.assertEqual(ra.scheduled_for(date(2026, 11, 26), timedelta(minutes=5), time(9, 0), TZ),
                         datetime(2026, 11, 25, 22, 45))


class RunTests(unittest.TestCase):
    def setUp(self):
        self.deliveries = {}   # (role, task_id, scheduled, target) -> status
        self.telegram, self.push = [], []
        self.tasks = {
            "following": [{"id": 5, "title": "Llamar al gestor", "due_date": date(2026, 11, 25), "due_time": None}],
            "agenda": [{"id": 8, "title": "Dentista", "due_date": date(2026, 11, 25), "due_time": None}],
        }
        self.subscriptions = [{"id": 3, "scope_path": "/gtd", "endpoint": "e", "p256dh": "k", "auth_key": "a",
                               "origin": "https://x"}]
        patches = [
            patch.object(reminders, "utc_now", return_value=NOW),
            patch.object(reminders, "within_notification_hours", return_value=True),
            patch.object(reminders, "send_telegram", side_effect=lambda token, chat, text: self.telegram.append(text)),
            patch.object(ra, "send_browser_push", side_effect=lambda sub, payload: self.push.append((sub["id"], payload))),
            patch.object(ra, "_candidates", side_effect=lambda role, today: self.tasks[role]),
            patch.object(ra, "q", side_effect=lambda sql, params=(): self.subscriptions),
            patch.object(ra, "q1", side_effect=self.q1),
            patch.object(ra, "exec_sql", side_effect=self.exec_sql),
            patch.object(ra, "commit"), patch.object(ra, "rollback"),
            patch.object(ra, "role_tag", side_effect=lambda role: {"following": "EnSeguimiento", "agenda": "agenda"}[role]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def q1(self, sql, params=()):
        if "GET_LOCK" in sql or "RELEASE_LOCK" in sql:
            return {"ok": 1}
        status = self.deliveries.get(tuple(params))
        return {"status": status} if status else None

    def exec_sql(self, sql, params=()):
        if sql.startswith("INSERT INTO role_alert_deliveries"):
            role, task_id, scheduled, target, status = params[:5]
            self.deliveries[(role, task_id, scheduled, target)] = status

    def run_alerts(self, cfg=CFG):
        return ra.run_role_alerts(TZ, time(8), time(20), lambda: "token", lambda: 1, "https://app/gtd", cfg=cfg)

    def test_sends_once_per_channel_and_device(self):
        self.assertEqual(self.run_alerts(), {"sent": 3, "failed": 0})
        self.assertEqual(self.deliveries[("following", 5, SCHEDULED, "app")], "sent")  # entrada de la campana
        self.assertEqual(len(self.telegram), 1)
        self.assertIn("EnSeguimiento: llega la fecha", self.telegram[0])
        self.assertIn("https://app/gtd/tasks/5/edit", self.telegram[0])
        self.assertEqual((self.push[0][0], self.push[0][1]["url"]), (3, "/gtd/tasks/5/edit"))
        self.assertEqual(self.run_alerts(), {"sent": 0, "failed": 0})  # segunda pasada: nada nuevo
        self.assertEqual(len(self.telegram), 1)

    def test_agenda_disabled_by_default_and_enabled_per_role(self):
        self.run_alerts()
        self.assertNotIn(("agenda", 8, SCHEDULED, "app"), self.deliveries)
        cfg = {"app": {"notifications": {"following": {"enabled": False},
                                         "agenda": {"enabled": True, "telegram": False, "browser": False, "app": True}}}}
        self.assertEqual(self.run_alerts(cfg), {"sent": 1, "failed": 0})
        self.assertEqual(self.deliveries[("agenda", 8, SCHEDULED, "app")], "sent")
        self.assertEqual(len(self.telegram), 1)  # solo el de EnSeguimiento de la primera pasada

    def test_channels_are_independent(self):
        cfg = {"app": {"notifications": {"following": {**ALL_ON, "telegram": False, "app": False}}}}
        self.assertEqual(self.run_alerts(cfg), {"sent": 1, "failed": 0})
        self.assertEqual((self.telegram, len(self.push)), ([], 1))

    def test_timed_task_alerts_in_the_previous_worker_slot(self):
        # son las 10:00 en Madrid: una tarea a las 10:25 (aviso 10:15) todavía no avisa;
        # una a las 10:20 (10:10 -> tramo de las 10:00) sí
        self.tasks["following"][0]["due_time"] = timedelta(hours=10, minutes=25)
        self.assertEqual(self.run_alerts(), {"sent": 0, "failed": 0})
        self.tasks["following"][0]["due_time"] = timedelta(hours=10, minutes=20)
        self.assertEqual(self.run_alerts()["sent"], 3)

    def test_not_before_scheduled_time_nor_after_retry_window(self):
        self.tasks["following"][0]["due_time"] = timedelta(hours=18)
        self.assertEqual(self.run_alerts(), {"sent": 0, "failed": 0})
        self.tasks["following"][0].update(due_date=date(2026, 11, 24), due_time=timedelta(hours=8))  # hace 26 h
        self.assertEqual(self.run_alerts(), {"sent": 0, "failed": 0})

    def test_postponed_task_is_notified_again(self):
        self.run_alerts()
        self.tasks["following"][0]["due_time"] = timedelta(hours=9, minutes=30)  # nueva hora ya pasada
        self.assertEqual(self.run_alerts()["sent"], 3)

    def test_disabled_or_without_channels(self):
        for following in ({"enabled": False}, {"telegram": False, "browser": False, "app": False}):
            with self.subTest(following=following):
                self.assertEqual(self.run_alerts({"app": {"notifications": {"following": following}}}),
                                 {"sent": 0, "failed": 0})
        self.assertEqual(self.deliveries, {})

    def test_telegram_failure_is_retried(self):
        with patch.object(reminders, "send_telegram", side_effect=RuntimeError("caído")):
            self.assertEqual(self.run_alerts()["failed"], 1)
        self.assertEqual(self.deliveries[("following", 5, SCHEDULED, "telegram")], "failed")
        self.run_alerts()
        self.assertEqual(len(self.telegram), 1)

    def test_expired_push_subscription_is_not_retried(self):
        from requests import Response
        from pywebpush import WebPushException
        response = Response()
        response.status_code = 410
        with patch.object(ra, "send_browser_push", side_effect=WebPushException("gone", response=response)):
            self.run_alerts()
        self.assertEqual(self.deliveries[("following", 5, SCHEDULED, "push:3")], "invalid")
        self.run_alerts()
        self.assertEqual(self.push, [])


class BellTests(unittest.TestCase):
    def test_bell_items_include_other_channels(self):
        app_row = {"id": 11, "role": "following", "task_id": 5, "scheduled_at_utc": SCHEDULED,
                   "updated_at_utc": NOW, "read_at_utc": None, "title": "Llamar al gestor"}
        others = [{"role": "following", "task_id": 5, "scheduled_at_utc": SCHEDULED, "target": "telegram", "device_name": None},
                  {"role": "following", "task_id": 5, "scheduled_at_utc": SCHEDULED, "target": "push:3", "device_name": "Móvil"}]
        with patch.object(ra, "q", side_effect=[[app_row], others]), patch.object(ra, "role_tag", return_value="EnSeguimiento"):
            items = ra.bell_items(NOW - timedelta(days=1))
        self.assertEqual(items[0]["channels"], ["App", "Telegram", "Navegador: Móvil"])
        self.assertEqual(items[0]["label"], "👀 EnSeguimiento")

    def test_history_endpoint_lists_alerts(self):
        alert = {"id": 11, "task_id": 5, "label": "👀 EnSeguimiento", "title": "Llamar al gestor",
                 "read_at_utc": None, "channels": ["App"], "updated_at_utc": NOW}
        with app.app.test_request_context("/api/notifications"), \
             patch_all("q", return_value=[]), patch_all("bell_items", return_value=[alert]), \
             patch_all("load_config", return_value={"app": {"timezone": "Europe/Madrid"}}):
            data = notifications_views.notification_history().get_json()["notifications"]
        self.assertEqual(data[0]["alert_id"], 11)
        self.assertEqual(data[0]["title"], "👀 EnSeguimiento · Llamar al gestor")
        self.assertEqual((data[0]["read"], data[0]["url"]), (False, "/tasks/5/edit"))

    def mark(self, payload, found=True):
        with app.app.test_request_context("/api/notifications/read", method="POST", json=payload,
                                          headers={"X-GTD-Notification-CSRF": "token"}), \
             patch_all("mark_bell_item_read", return_value=found) as mark, patch_all("commit") as commit:
            session["notification_csrf"] = "token"
            response = notifications_views.notification_mark_read()
        return response, mark, commit

    def test_mark_alert_read(self):
        response, mark, commit = self.mark({"alert_id": 11})
        mark.assert_called_once_with(11)
        commit.assert_called_once()
        self.assertEqual(response.get_json(), {"ok": True})

    def test_mark_alert_read_errors(self):
        self.assertEqual(self.mark({"alert_id": "11"})[0][1], 400)
        self.assertEqual(self.mark({"alert_id": 11}, found=False)[0][1], 404)


class AdminSettingsTests(unittest.TestCase):
    def post(self, data):
        with patch.dict(os.environ, {"GTD_ADMIN_PASSWORD": "x"}), \
             app.app.test_request_context("/admin", method="POST", data=dict(action="save_role_alerts", **data)), \
             patch_all("admin_required", return_value=True), \
             patch_all("load_config", return_value={}), patch_all("save_config") as save:
            response = admin_views.admin()
        return response, save

    def test_saves_both_roles(self):
        response, save = self.post({"following_enabled": "1", "following_time": "07:30", "following_browser": "1",
                                    "following_app": "1", "agenda_time": "08:00", "agenda_telegram": "1"})
        self.assertEqual(response.status_code, 302)
        notifications = save.call_args.args[0]["app"]["notifications"]
        self.assertEqual(notifications["following"], {"enabled": True, "time": "07:30", "telegram": False,
                                                      "browser": True, "app": True})
        self.assertEqual(notifications["agenda"], {"enabled": False, "time": "08:00", "telegram": True,
                                                   "browser": False, "app": False})

    def test_rejects_invalid_time(self):
        _response, save = self.post({"following_time": "7h", "agenda_time": "08:00"})
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
