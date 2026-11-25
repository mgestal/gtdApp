"""Respuestas JSON para actualizar la página sin recargar (completar, etiquetar, mover)."""
import unittest
from datetime import date
from unittest.mock import patch

import app as application
from gtd import tasks_ops
from gtd.views import folders as folders_views
from gtd.views import projects as projects_views
from gtd.views import tags as tags_views
from gtd.views import tasks as tasks_views
from tests.support import patch_all

COUNTS = {"inbox": 3, "today": 1, "week": 2, "next": 4, "agenda": 5, "projects": 6, "tags": 7, "filters": 8}
FETCH = {"X-Requested-With": "fetch"}


def task_row(**extra):
    row = {"id": 5, "completed_at": None, "due_date": None, "recurrence_rule": None, "max_occurrences": None,
           "project_id": None, "archived": 0, "last_next_due_date": None}
    row.update(extra)
    return row


class ToggleJsonTests(unittest.TestCase):
    def toggle(self, task, headers=FETCH, promoted=False):
        with application.app.test_request_context("/tasks/5/toggle", method="POST", headers=headers), \
             patch_all("q1", return_value=task), \
             patch_all("complete_nonrecurring_task", return_value=promoted) as complete, \
             patch_all("complete_recurring_task_run") as recurring, \
             patch_all("exec_sql"), patch_all("_project_board_status_for_task"), \
             patch_all("_mark_task_calendar_dirty"), patch_all("commit"), \
             patch_all("inject_sidebar_counts", return_value={"sidebar_counts": COUNTS}):
            response = tasks_views.task_toggle(5)
        return response, complete, recurring

    def test_complete_returns_json_without_reload(self):
        response, complete, _ = self.toggle(task_row())
        complete.assert_called_once()
        self.assertEqual(response.get_json(), {"ok": True, "task_id": 5, "completed": True, "reload": False,
                                               "counts": COUNTS})

    def test_uncomplete(self):
        response, complete, _ = self.toggle(task_row(completed_at="2026-11-25 10:00"))
        complete.assert_not_called()
        self.assertEqual((response.get_json()["completed"], response.get_json()["reload"]), (False, False))

    def test_periodic_task_asks_for_reload(self):
        response, _, recurring = self.toggle(task_row(recurrence_rule="FREQ=DAILY", due_date=date(2026, 11, 25)))
        recurring.assert_called_once()
        self.assertTrue(response.get_json()["reload"])

    def test_nextaction_promotion_asks_for_reload(self):
        response, _, _ = self.toggle(task_row(project_id=9), promoted=True)
        self.assertTrue(response.get_json()["reload"])

    def test_classic_form_still_redirects(self):
        response, _, _ = self.toggle(task_row(), headers={})
        self.assertEqual(response.status_code, 302)


class CompleteNonrecurringPromotionTests(unittest.TestCase):
    def test_returns_whether_another_task_was_promoted(self):
        cases = [
            # (tenía NextAction, proyecto, siguiente tarea, esperado)
            (False, 9, {"id": 6}, False),
            (True, None, {"id": 6}, False),
            (True, 9, None, False),
            (True, 9, {"id": 6}, True),
        ]
        for had_next, project_id, next_task, expected in cases:
            with self.subTest(had_next=had_next, project_id=project_id, next_task=next_task), \
                 patch.object(tasks_ops, "task_has_role_tag", side_effect=lambda tid, role: had_next and tid == 5), \
                 patch.object(tasks_ops, "exec_sql"), patch.object(tasks_ops, "_project_board_status_for_task"), \
                 patch.object(tasks_ops, "cfg_bool", return_value=True), \
                 patch.object(tasks_ops, "q1", side_effect=lambda sql, params=(): (
                     {"auto_promote_nextaction": None} if "auto_promote" in sql else next_task)), \
                 patch.object(tasks_ops, "role_tag_id", return_value=1):
                promoted = tasks_ops.complete_nonrecurring_task(5, {"project_id": project_id}, None)
            self.assertIs(promoted, expected)


class TagAndMoveJsonTests(unittest.TestCase):
    def test_add_tag_returns_tag_and_counts(self):
        def q1(sql, params=()):
            return {"id": 3, "name": "Casa"} if "FROM tags" in sql else {"id": 5}
        with application.app.test_request_context("/api/tasks/5/add_tag", method="POST", json={"tag_id": 3}), \
             patch_all("q1", q1), patch_all("exec_sql"), patch_all("commit"), \
             patch_all("inject_sidebar_counts", return_value={"sidebar_counts": COUNTS}):
            data = tags_views.api_task_add_tag(5).get_json()
        self.assertEqual(data, {"ok": True, "tag": {"id": 3, "name": "Casa", "url": "/tags/3"}, "counts": COUNTS})

    def test_move_returns_counts(self):
        with application.app.test_request_context("/api/tasks/5/move", method="POST",
                                                  json={"target_type": "folder", "target_id": 2}), \
             patch_all("q1", return_value={"id": 2}), patch_all("exec_sql"), patch_all("commit"), \
             patch_all("inject_sidebar_counts", return_value={"sidebar_counts": COUNTS}):
            data = tasks_views.api_task_move(5).get_json()
        self.assertEqual((data["ok"], data["counts"]), (True, COUNTS))

    def test_project_board_returns_completed_and_tags(self):
        def q1(sql, params=()):
            if "FROM projects" in sql:
                return {"id": 9, "board_columns": None}
            if "project_board_status FROM tasks" in sql:
                return {"project_board_status": "todo"}
            return {"id": 5, "project_id": 9}
        with application.app.test_request_context("/api/projects/9/board/tasks/5/status", method="POST",
                                                  json={"status": "in-progress"}), \
             patch_all("q1", q1), patch_all("exec_sql"), patch_all("commit"), \
             patch_all("_project_board_columns", return_value=[{"key": "in-progress"}, {"key": "done"}]), \
             patch_all("get_or_create_tag", return_value=1), \
             patch_all("load_tags_map", return_value={5: [{"id": 1, "name": "EnCurso"}]}), \
             patch_all("inject_sidebar_counts", return_value={"sidebar_counts": COUNTS}):
            data = projects_views.api_project_board_task_status(9, 5).get_json()
        self.assertEqual((data["completed"], data["tags"]), (False, ["EnCurso"]))


class ViewScopeTests(unittest.TestCase):
    """Las vistas filtradas por ubicación publican su ámbito para quitar filas al mover."""

    def render_folder(self, url):
        with application.app.test_request_context(url), \
             patch_all("q1", return_value={"id": 1, "name": "Casa", "parent_id": None}), \
             patch_all("q", return_value=[]), \
             patch_all("build_folder_breadcrumb", return_value=[]), \
             patch_all("get_folder_tree_ids", return_value={1, 4}), \
             patch_all("load_tags_map", return_value={}), \
             patch_all("load_subtasks_map", return_value={}), patch_all("load_subtask_counts", return_value={}), \
             patch_all("render_template", side_effect=lambda name, **ctx: ctx):
            return folders_views.folder_detail(1)["view_scope"]

    def test_folder_scope(self):
        self.assertEqual(self.render_folder("/folders/1"),
                         {"task_folders": [1], "task_projects": [], "project_folders": [1]})

    def test_folder_scope_with_subfolders(self):
        self.assertEqual(self.render_folder("/folders/1?subfolders=1"),
                         {"task_folders": [1, 4], "task_projects": [], "project_folders": [1]})


if __name__ == "__main__":
    unittest.main()
