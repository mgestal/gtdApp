"""Procesar Inbox: acción «Convertir en proyecto» (action=project)."""
import unittest
from datetime import date

from flask import session

import app as application
from gtd.views import inbox as inbox_views
from tests.support import patch_all

TASK = {"id": 10, "title": "Organizar viaje", "notes": "  Ideas sueltas  ", "completed_at": None,
        "due_date": None, "recurrence_rule": None, "max_occurrences": None, "project_id": None,
        "folder_id": None, "archived": 0, "last_next_due_date": None}
NEXT_TAG_ID = 77


class FakeExec:
    """Devuelve ids crecientes para los INSERT y registra todas las sentencias."""

    def __init__(self):
        self.calls = []
        self.next_id = 500

    def __call__(self, sql, params=()):
        self.calls.append((sql, params))
        if sql.startswith("INSERT INTO projects") or sql.startswith("INSERT INTO tasks"):
            self.next_id += 1
            return self.next_id
        return 0

    def find(self, prefix):
        return [c for c in self.calls if c[0].startswith(prefix)]


class InboxConvertToProjectTests(unittest.TestCase):
    def _run(self, form, q1_results=None, existing_project=None):
        fake = FakeExec()
        data = {"action": "project", "title": TASK["title"], **form}
        with application.app.test_request_context("/inbox/process/10", method="POST", data=data), \
             patch_all("q1", side_effect=list(q1_results or [dict(TASK)])), \
             patch_all("exec_sql", new=fake), \
             patch_all("find_project_by_name_active", return_value=existing_project), \
             patch_all("role_tag_id", return_value=NEXT_TAG_ID), \
             patch_all("next_project_task_sort_order", return_value=1), \
             patch_all("_mark_task_calendar_dirty") as dirty, \
             patch_all("commit") as commit, \
             patch_all("rollback") as rollback:
            response = inbox_views.inbox_process_action(10)
            flashes = session.get("_flashes", [])
        return response, fake, commit, rollback, dirty, flashes

    def test_steps_create_project_tasks_and_trash_original(self):
        response, fake, commit, _, dirty, flashes = self._run(
            {"destination": "folder:3", "steps": "- Reservar hotel 15-12-2026\n\n* Comprar billetes"},
            q1_results=[dict(TASK), {"id": 3}],
        )
        self.assertEqual(response.status_code, 302)
        commit.assert_called_once()

        (project_sql, project_params), = fake.find("INSERT INTO projects")
        self.assertEqual(project_params, ("Organizar viaje", "Ideas sueltas", 3))
        steps = fake.find("INSERT INTO tasks")
        self.assertEqual([p[:3] for _, p in steps],
                         [("Reservar hotel", 501, date(2026, 12, 15)), ("Comprar billetes", 501, None)])
        trash = [c for c in fake.find("UPDATE tasks") if "deleted_at=NOW()" in c[0]]
        self.assertEqual(trash[0][1], (0, 10))
        tag, = fake.find("INSERT IGNORE INTO task_tags")
        self.assertEqual(tag[1], (502, NEXT_TAG_ID))
        self.assertEqual({c.args[0] for c in dirty.call_args_list}, {502, 503, 10})
        self.assertEqual(flashes[-1][0], "ok")
        self.assertIn("2 pasos", flashes[-1][1])

    def test_without_steps_moves_original_task_as_next_action(self):
        _, fake, commit, _, _, flashes = self._run({"steps": "  \n"})
        commit.assert_called_once()
        (_, project_params), = fake.find("INSERT INTO projects")
        self.assertEqual(project_params[2], None)  # sin destino: proyecto sin carpeta
        self.assertEqual(fake.find("INSERT INTO tasks"), [])
        move, = [c for c in fake.find("UPDATE tasks") if "project_id=%s" in c[0]]
        self.assertEqual(move[1], (501, 1, 10))
        self.assertFalse([c for c in fake.calls if "deleted_at=NOW()" in c[0]])
        tag, = fake.find("INSERT IGNORE INTO task_tags")
        self.assertEqual(tag[1], (10, NEXT_TAG_ID))
        self.assertEqual(flashes[-1][0], "ok")

    def test_edited_title_names_the_project(self):
        _, fake, _, _, _, _ = self._run({"title": "  Viaje a Roma ", "steps": ""})
        (_, project_params), = fake.find("INSERT INTO projects")
        self.assertEqual(project_params[0], "Viaje a Roma")

    def test_project_destination_is_rejected(self):
        _, fake, commit, _, _, flashes = self._run({"destination": "project:4", "steps": "Paso"})
        self.assertEqual(fake.calls, [])
        commit.assert_not_called()
        self.assertEqual(flashes[-1][0], "error")

    def test_existing_active_project_name_is_rejected(self):
        _, fake, commit, _, _, flashes = self._run({"steps": "Paso"}, existing_project=8)
        self.assertEqual(fake.calls, [])
        commit.assert_not_called()
        self.assertIn("Ya existe", flashes[-1][1])

    def test_invalid_step_date_creates_nothing(self):
        _, fake, commit, _, _, flashes = self._run({"steps": "Paso uno\nPaso dos 31-02-2026"})
        self.assertEqual(fake.calls, [])
        commit.assert_not_called()
        self.assertEqual(flashes[-1][0], "error")

    def test_database_error_rolls_back(self):
        with application.app.test_request_context("/inbox/process/10", method="POST",
                                                  data={"action": "project", "steps": "Paso"}), \
             patch_all("q1", side_effect=[dict(TASK)]), \
             patch_all("exec_sql", side_effect=Exception("Duplicate entry")), \
             patch_all("find_project_by_name_active", return_value=None), \
             patch_all("commit") as commit, \
             patch_all("rollback") as rollback:
            inbox_views.inbox_process_action(10)
        rollback.assert_called_once()
        commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
