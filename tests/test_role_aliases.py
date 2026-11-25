"""Los alias configurados para un rol GTD se reconocen en las consultas (p.ej. revisión semanal)."""
import unittest
from unittest.mock import patch

from tests.support import patch_all
from gtd import roles

ROLES = {
    "tags": {"waiting": ["EnEspera", "WaitingFor"], "next_action": ["NextAction"]},
    "folders": {},
}


class RoleTagAliasTests(unittest.TestCase):
    def setUp(self):
        p = patch.object(roles, "gtd_roles", return_value=ROLES)
        p.start()
        self.addCleanup(p.stop)

    def test_filter_includes_canonical_and_aliases(self):
        clause, params = roles.role_tag_filter("waiting")
        self.assertEqual(params, ("enespera", "waitingfor"))
        self.assertIn("AND EXISTS (", clause)
        self.assertIn("rtt.task_id=t.id", clause)
        self.assertEqual(clause.count("%s"), len(params))

    def test_filter_uses_given_task_alias(self):
        clause, _ = roles.role_tag_filter("next_action", task_alias="x")
        self.assertIn("rtt.task_id=x.id", clause)

    def test_exists_checks_every_alias(self):
        with patch_all("q1", return_value={"id": 7}) as q1:
            self.assertTrue(roles.role_tag_exists("waiting"))
        sql, params = q1.call_args.args
        self.assertIn("LOWER(name) IN (%s,%s)", sql)
        self.assertEqual(params, ("enespera", "waitingfor"))

    def test_exists_false_when_no_tag(self):
        with patch_all("q1", return_value=None):
            self.assertFalse(roles.role_tag_exists("waiting"))


class ReviewUsesAliasesTests(unittest.TestCase):
    def test_review_queries_filter_by_all_aliases(self):
        import app as application
        from gtd.views import review as review_views

        with application.app.test_request_context("/review"), \
             patch.object(roles, "gtd_roles", return_value={
                 "tags": {"next_action": ["NextAction"], "following": ["EnSeguimiento"],
                          "agenda": ["agenda"], "waiting": ["EnEspera", "WaitingFor"]},
                 "folders": {"someday": [], "following": [], "waiting": [], "checklists": [],
                             "adtv": [], "not_this_week": [], "agenda": []},
             }), \
             patch_all("role_tag_exists", return_value=True), \
             patch_all("role_folder_tree_ids", return_value=set()), \
             patch_all("role_folder_row", return_value=None), \
             patch_all("role_folder_exists", return_value=False), \
             patch_all("q", return_value=[]) as q, \
             patch_all("q1", return_value=None), \
             patch_all("load_tags_map", return_value={}), \
             patch_all("render_template", return_value="ok"):
            review_views.review()

        waiting_calls = [c for c in q.call_args_list
                         if len(c.args) > 1 and "waitingfor" in c.args[1]]
        self.assertTrue(waiting_calls, "La consulta de EnEspera no incluye el alias WaitingFor")
        for c in waiting_calls:
            self.assertIn("enespera", c.args[1])
            self.assertNotIn("JOIN task_tags tt", c.args[0])


if __name__ == "__main__":
    unittest.main()
