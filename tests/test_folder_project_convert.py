"""Conversión carpeta → proyecto y proyecto → carpeta."""
import unittest

from tests.support import patch_all
from gtd.views import folders as folder_views
from gtd.views import projects as project_views


def _sql(mock):
    return [c.args[0] for c in mock.call_args_list]


class FolderToProjectTests(unittest.TestCase):
    def _run(self, q1_side_effect, exec_return=91):
        import app as application

        with application.app.test_request_context("/folders/5/convert_to_project", method="POST"), \
             patch_all("q1", side_effect=q1_side_effect), \
             patch_all("exec_sql", return_value=exec_return) as exec_sql, \
             patch_all("commit") as commit, \
             patch_all("rollback") as rollback, \
             patch_all("flash"):
            response = folder_views.folder_convert_to_project(5)
        return response, exec_sql, commit, rollback

    def test_converts_empty_folder(self):
        folder = {"id": 5, "name": "Viajes", "parent_id": 2}
        response, exec_sql, commit, _ = self._run([folder, None, None])

        self.assertTrue(response.location.endswith("/projects/91"))
        insert, move, delete = exec_sql.call_args_list
        self.assertIn("INSERT INTO projects", insert.args[0])
        self.assertEqual(insert.args[1], ("Viajes", 2))
        self.assertIn("UPDATE tasks SET project_id=%s, folder_id=NULL", move.args[0])
        self.assertEqual(move.args[1], (91, 5))
        self.assertIn("DELETE FROM folders", delete.args[0])
        commit.assert_called_once()

    def test_refuses_folder_with_subfolders(self):
        folder = {"id": 5, "name": "Viajes", "parent_id": None}
        response, exec_sql, commit, _ = self._run([folder, {"ok": 1}, None])

        self.assertTrue(response.location.endswith("/folders/5"))
        exec_sql.assert_not_called()
        commit.assert_not_called()

    def test_refuses_folder_with_projects(self):
        folder = {"id": 5, "name": "Viajes", "parent_id": None}
        response, exec_sql, _, _ = self._run([folder, None, {"ok": 1}])

        self.assertTrue(response.location.endswith("/folders/5"))
        exec_sql.assert_not_called()

    def test_rolls_back_on_error(self):
        import app as application

        folder = {"id": 5, "name": "Viajes", "parent_id": None}
        with application.app.test_request_context("/folders/5/convert_to_project", method="POST"), \
             patch_all("q1", side_effect=[folder, None, None]), \
             patch_all("exec_sql", side_effect=Exception("Duplicate entry")), \
             patch_all("commit") as commit, \
             patch_all("rollback") as rollback, \
             patch_all("flash"):
            response = folder_views.folder_convert_to_project(5)

        self.assertTrue(response.location.endswith("/folders/5"))
        rollback.assert_called_once()
        commit.assert_not_called()


class ProjectToFolderTests(unittest.TestCase):
    def _run(self, project, exec_return=44):
        import app as application

        with application.app.test_request_context("/projects/8/convert_to_folder", method="POST"), \
             patch_all("q1", return_value=project), \
             patch_all("exec_sql", return_value=exec_return) as exec_sql, \
             patch_all("commit") as commit, \
             patch_all("rollback"), \
             patch_all("flash"):
            response = project_views.project_convert_to_folder(8)
        return response, exec_sql, commit

    def test_converts_active_project(self):
        project = {"id": 8, "name": "Citas", "folder_id": 2, "archived": 0, "deleted_at": None}
        response, exec_sql, commit = self._run(project)

        self.assertTrue(response.location.endswith("/folders/44"))
        insert, move, delete = exec_sql.call_args_list
        self.assertIn("INSERT INTO folders", insert.args[0])
        self.assertEqual(insert.args[1], ("Citas", 2))
        self.assertIn("SET folder_id=%s, project_id=NULL", move.args[0])
        self.assertIn("project_board_status=NULL", move.args[0])
        self.assertEqual(move.args[1], (44, 8))
        self.assertIn("DELETE FROM projects", delete.args[0])
        commit.assert_called_once()

    def test_refuses_archived_project(self):
        project = {"id": 8, "name": "Citas", "folder_id": 2, "archived": 1, "deleted_at": None}
        response, exec_sql, commit = self._run(project)

        self.assertTrue(response.location.endswith("/projects/8"))
        exec_sql.assert_not_called()
        commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
