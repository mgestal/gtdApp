"""Tests del estado de la revisión semanal (gtd/weekly_review.py y rutas /review/*)."""
import unittest
from datetime import date, datetime
from unittest.mock import patch

from flask import session

import app as application
from gtd import weekly_review as wr
from gtd.views import review as review_views
from tests.support import patch_all

NOW = datetime(2026, 11, 25, 18, 30)


class FakeDb:
    """Simula la tabla weekly_reviews para set_block/finish_review."""

    def __init__(self, rows=None):
        self.rows = rows or []

    def current(self):
        open_rows = [r for r in self.rows if r["finished_at"] is None]
        return dict(open_rows[-1]) if open_rows else None

    def exec_sql(self, sql, params=()):
        if sql.startswith("INSERT"):
            row = {"id": len(self.rows) + 1, "started_at": params[0], "finished_at": None, "checked_blocks": ""}
            self.rows.append(row)
            return row["id"]
        row = next(r for r in self.rows if r["id"] == params[1])
        if "checked_blocks" in sql:
            row["checked_blocks"] = params[0]
        else:
            row["finished_at"] = params[0]
        return 0


class ReviewStateLogicTests(unittest.TestCase):
    def setUp(self):
        self.db = FakeDb()
        for target, new in (("current_review", self.db.current), ("exec_sql", self.db.exec_sql),
                            ("_now", lambda: NOW)):
            p = patch.object(wr, target, new)
            p.start()
            self.addCleanup(p.stop)

    def test_parse_blocks_keeps_checklist_order_and_ignores_unknown(self):
        self.assertEqual(wr.parse_blocks("projects, inbox,xx,,inbox"), ["inbox", "projects"])
        self.assertEqual(wr.parse_blocks(None), [])

    def test_first_check_starts_a_review(self):
        self.assertEqual(wr.set_block("waiting", True), ["waiting"])
        self.assertEqual(len(self.db.rows), 1)
        self.assertEqual(self.db.rows[0]["started_at"], NOW)
        self.assertEqual(wr.set_block("inbox", True), ["inbox", "waiting"])
        self.assertEqual(len(self.db.rows), 1)

    def test_uncheck_without_review_does_not_start_one(self):
        self.assertEqual(wr.set_block("inbox", False), [])
        self.assertEqual(self.db.rows, [])

    def test_uncheck_removes_block(self):
        wr.set_block("inbox", True)
        wr.set_block("projects", True)
        self.assertEqual(wr.set_block("inbox", False), ["projects"])
        self.assertEqual(self.db.rows[0]["checked_blocks"], "projects")

    def test_unknown_block_is_rejected(self):
        with self.assertRaises(ValueError):
            wr.set_block("nope", True)

    def test_finish_closes_review_and_next_check_starts_a_new_one(self):
        self.assertIsNone(wr.finish_review())
        wr.set_block("inbox", True)
        self.assertEqual(wr.finish_review(), NOW)
        self.assertEqual(self.db.rows[0]["finished_at"], NOW)
        wr.set_block("projects", True)
        self.assertEqual(len(self.db.rows), 2)
        self.assertEqual(self.db.rows[1]["checked_blocks"], "projects")

    def test_load_state(self):
        def q1(sql, params=()):
            if "finished_at IS NULL" in sql:
                return {"id": 7, "started_at": NOW, "checked_blocks": "inbox,brainstorm"}
            return {"finished_at": datetime(2026, 11, 18, 21, 0)}
        with patch.object(wr, "q1", q1), patch.object(wr, "current_review", lambda: q1("finished_at IS NULL")):
            state = wr.load_state(today=date(2026, 11, 25))
        self.assertTrue(state.in_progress)
        self.assertEqual((state.checked, state.total), (["inbox", "brainstorm"], 9))
        self.assertEqual(state.days_since_last, 7)

    def test_load_state_without_reviews(self):
        with patch.object(wr, "q1", lambda *a: None), patch.object(wr, "current_review", lambda: None):
            state = wr.load_state(today=date(2026, 11, 25))
        self.assertFalse(state.in_progress)
        self.assertIsNone(state.last_finished_at)


class ProjectsWithoutIssuesTests(unittest.TestCase):
    def test_flagged_projects_are_removed_from_active(self):
        active = [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}, {"id": 3, "name": "C"}, {"id": 4, "name": "D"}]
        without_next = [{"id": 2, "name": "B", "pending": 5}]
        empty = [{"id": 4, "name": "D"}]
        self.assertEqual([p["id"] for p in wr.projects_without_issues(active, without_next, empty)], [1, 3])

    def test_no_flags_keeps_all(self):
        active = [{"id": 1}, {"id": 2}]
        self.assertEqual(wr.projects_without_issues(active, [], []), active)


class StalledProjectsTests(unittest.TestCase):
    def _run(self, rows, days=14, excluded=frozenset({9, 4})):
        calls = []

        def q(sql, params=()):
            calls.append((sql, params))
            return [dict(r) for r in rows]

        with patch.object(wr, "q", q), patch.object(wr, "non_active_project_folder_ids", lambda: set(excluded)):
            result = wr.stalled_projects(days, now=NOW)
        return result, calls

    def test_cutoff_and_excluded_folders_are_query_params(self):
        _, calls = self._run([])
        (sql, params), = calls
        self.assertEqual(params, (4, 9, datetime(2026, 11, 11, 18, 30)))
        self.assertIn("NOT IN (%s,%s)", sql)
        self.assertIn("HAVING last_activity < %s", sql)
        self.assertIn("a.pending > 0", sql)
        self.assertIn("t.deleted_at IS NULL", sql)

    def test_without_excluded_folders(self):
        _, calls = self._run([], days=7, excluded=frozenset())
        (sql, params), = calls
        self.assertNotIn("NOT IN", sql)
        self.assertEqual(params, (datetime(2026, 11, 18, 18, 30),))

    def test_rows_get_idle_days_and_int_pending(self):
        from decimal import Decimal
        rows = [{"id": 1, "name": "A", "folder_name": None, "pending": Decimal("3"),
                 "last_activity": datetime(2026, 10, 26, 9, 0)}]
        result, _ = self._run(rows)
        self.assertEqual(result[0]["idle_days"], 30)
        self.assertEqual(result[0]["pending"], 3)

    def test_days_come_from_config_with_limits(self):
        cases = ((None, 14), ({"stalled_project_days": 21}, 21), ({"stalled_project_days": 0}, 1),
                 ({"stalled_project_days": 9999}, wr.STALLED_PROJECT_DAYS_MAX),
                 ({"stalled_project_days": "x"}, 14))
        for review_cfg, expected in cases:
            cfg = {"app": {"review": review_cfg}} if review_cfg is not None else {"app": {}}
            with self.subTest(review_cfg=review_cfg), application.app.test_request_context("/review"):
                from flask import g
                g.cfg = cfg
                self.assertEqual(wr.stalled_project_days(), expected)

    def test_stalled_projects_are_removed_from_active(self):
        active = [{"id": 1}, {"id": 2}, {"id": 3}]
        self.assertEqual([p["id"] for p in wr.projects_without_issues(active, [], [{"id": 2}], [])], [1, 3])


class ReviewSettingsAdminTests(unittest.TestCase):
    def _post(self, value):
        from gtd.views import admin as admin_views
        saved = []
        with application.app.test_request_context("/admin", method="POST",
                                                  data={"action": "save_review_settings",
                                                        "stalled_project_days": value}), \
             patch_all("load_config", return_value={"app": {"timezone": "Europe/Madrid"}}), \
             patch_all("save_config", side_effect=saved.append), \
             patch_all("admin_required", return_value=True):
            response = admin_views.admin()
            flashes = session.get("_flashes", [])
        return response, saved, flashes

    def test_saves_days(self):
        response, saved, flashes = self._post("21")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("#review-settings"))
        self.assertEqual(saved, [{"app": {"timezone": "Europe/Madrid", "review": {"stalled_project_days": 21}}}])
        self.assertEqual(flashes[-1][0], "ok")

    def test_rejects_invalid_days(self):
        for value in ("0", "abc", "", str(wr.STALLED_PROJECT_DAYS_MAX + 1)):
            with self.subTest(value=value):
                _, saved, flashes = self._post(value)
                self.assertEqual(saved, [])
                self.assertEqual(flashes[-1][0], "error")


class ReviewRoutesTests(unittest.TestCase):
    def test_toggle_block(self):
        with application.app.test_request_context("/review/blocks/inbox", method="POST", json={"checked": True}), \
             patch_all("set_block", return_value=["inbox"]) as set_block, patch_all("commit") as commit:
            response = review_views.review_block_toggle("inbox")
        set_block.assert_called_once_with("inbox", True)
        commit.assert_called_once()
        self.assertEqual(response.get_json(), {"ok": True, "checked": ["inbox"], "total": 9})

    def test_toggle_unknown_block(self):
        with application.app.test_request_context("/review/blocks/x", method="POST", json={"checked": True}):
            response, status = review_views.review_block_toggle("x")
        self.assertEqual(status, 404)

    def test_finish(self):
        for finished, category in ((NOW, "ok"), (None, "error")):
            with self.subTest(finished=finished), \
                 application.app.test_request_context("/review/finish", method="POST"), \
                 patch_all("finish_review", return_value=finished), patch_all("commit"):
                response = review_views.review_finish()
                self.assertEqual(response.status_code, 302)
                self.assertEqual(session["_flashes"][0][0], category)


if __name__ == "__main__":
    unittest.main()
