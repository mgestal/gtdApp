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
