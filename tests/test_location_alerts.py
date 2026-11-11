import unittest
from unittest.mock import patch

from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader, select_autoescape

import app as application


class LocationAlertTests(unittest.TestCase):
    def test_outside_hours_still_returns_locations_for_existing_matches(self):
        with application.app.test_request_context("/api/location-alerts"), \
             patch.object(application, "push_test_within_hours", return_value=False), \
             patch.object(application, "q", return_value=[]) as query:
            response = application.location_alerts()
        self.assertEqual(response.get_json(), {"within_hours": False, "locations": []})
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        query.assert_called_once()

    def test_active_task_query_and_subpath_link(self):
        row = {"id": 5, "name": "Casa", "latitude": 42.0, "longitude": -8.0,
               "radius_m": 200.0, "task_id": 12}
        with application.app.test_request_context("/api/location-alerts", environ_overrides={"SCRIPT_NAME": "/gtd"}), \
             patch.object(application, "push_test_within_hours", return_value=True), \
             patch.object(application, "q", return_value=[row]) as query:
            response = application.location_alerts()
        data = response.get_json()
        self.assertEqual(data["locations"][0]["url"], "/gtd/tags/5?location_pending=1")
        sql = query.call_args.args[0]
        for condition in ("t.completed_at IS NULL", "t.archived=0", "t.deleted_at IS NULL",
                          "p.archived=0", "p.deleted_at IS NULL"):
            self.assertIn(condition, sql)

    def post_tag(self, values):
        with application.app.test_request_context("/tags/5/edit", method="POST", data=values), \
             patch.object(application, "q1", side_effect=[{"id": 5, "name": "Casa", "type": None}, None]), \
             patch.object(application, "q", return_value=[]), \
             patch.object(application, "exec_sql") as execute, \
             patch.object(application, "commit") as commit:
            response = application.tag_edit(5)
        return response, execute, commit

    def test_save_and_disable_location(self):
        _, execute, commit = self.post_tag({"name": "Casa", "location_enabled": "on",
                                             "latitude": "42.1", "longitude": "-8.2", "radius_m": "200"})
        self.assertIn("INSERT INTO tag_locations", execute.call_args_list[-1].args[0])
        self.assertEqual(execute.call_args_list[-1].args[1], (5, 42.1, -8.2, 200.0))
        commit.assert_called_once()
        _, execute, commit = self.post_tag({"name": "Casa"})
        self.assertIn("DELETE FROM tag_locations", execute.call_args_list[-1].args[0])
        commit.assert_called_once()

    def test_invalid_coordinates_do_not_save(self):
        for latitude, longitude, radius in [
            ("nan", "0", "200"), ("91", "0", "200"), ("0", "181", "200"),
            ("0", "0", "0"), ("0", "0", "100001"), ("", "0", "200")
        ]:
            with self.subTest(latitude=latitude, longitude=longitude, radius=radius):
                _, execute, commit = self.post_tag({"name": "Casa", "location_enabled": "on",
                    "latitude": latitude, "longitude": longitude, "radius_m": radius})
                execute.assert_not_called()
                commit.assert_not_called()

    def test_location_link_filters_even_when_sidebar_includes_done_and_archived(self):
        with application.app.test_request_context("/tags/5?location_pending=1"), \
             patch.object(application, "q1", side_effect=[{"id": 5, "name": "Casa"}, {"c": 0}]) as query_one, \
             patch.object(application, "q", return_value=[]) as query, \
             patch.object(application, "cfg_int", return_value=25), \
             patch.object(application, "load_config", return_value={"sidebar": {"tag_task_visibility": {
                 "completed": True, "archived": True}}}), \
             patch.object(application, "load_tags_map", return_value={}), \
             patch.object(application, "load_subtask_counts", return_value={}), \
             patch.object(application, "load_subtasks_map", return_value={}), \
             patch.object(application, "render_template", side_effect=lambda name, **context: context):
            context = application.tag_detail(5)
        self.assertTrue(context["location_pending"])
        for sql in (query_one.call_args.args[0], query.call_args.args[0]):
            for condition in ("t.completed_at IS NULL", "t.archived=0", "t.deleted_at IS NULL",
                              "p.archived=0", "p.deleted_at IS NULL"):
                self.assertIn(condition, sql)

    def test_tag_list_and_sidebar_indicate_location_without_changing_counts(self):
        rows = [
            {"id": 5, "name": "Casa", "type": None, "task_count": 2, "has_location": 1},
            {"id": 6, "name": "Trabajo", "type": None, "task_count": 1, "has_location": 0},
        ]
        with application.app.test_request_context("/tags"), \
             patch.object(application, "q1", return_value={"c": 2}), \
             patch.object(application, "q", side_effect=[rows, []]) as query, \
             patch.object(application, "cfg_int", return_value=25), \
             patch.object(application, "render_template", side_effect=lambda name, **context: context):
            context = application.tags()
            sql = query.call_args_list[0].args[0]
            self.assertIn("LEFT JOIN tag_locations loc ON loc.tag_id=tg.id", sql)
            self.assertIn("COUNT(t.id) AS task_count", sql)
            env = Environment(loader=ChoiceLoader([
                DictLoader({"base.html": "{% block content %}{% endblock %}"}),
                FileSystemLoader(application.app.template_folder),
            ]), autoescape=select_autoescape(["html"]))
            env.globals["url_for"] = lambda endpoint, **kwargs: "/"
            html = env.get_template("tags.html").render(**context)
        self.assertEqual(html.count('aria-label="Recordatorio por ubicación configurado"'), 1)
        self.assertIn("2 tareas", html)
        self.assertIn("1 tareas", html)

        with application.app.test_request_context("/tags"), \
             patch.object(application, "load_config", return_value={}), \
             patch.object(application, "q", return_value=rows) as query:
            ordered = application._sidebar_tags_ordered()
            self.assertIn("LEFT JOIN tag_locations loc ON loc.tag_id=tg.id", query.call_args.args[0])
        with application.app.test_request_context("/tags"), \
             patch.object(application, "load_config", return_value={}), \
             patch.object(application, "_sidebar_filters_ordered", return_value=[]), \
             patch.object(application, "_sidebar_tags_ordered", return_value=ordered):
            groups = application.inject_sidebar_filters()["sidebar_tag_groups"]
        sidebar_tags = {tag["name"]: tag for group in groups for tag in group["tags"]}
        self.assertTrue(sidebar_tags["Casa"]["has_location"])
        self.assertFalse(sidebar_tags["Trabajo"]["has_location"])
        self.assertEqual(sidebar_tags["Casa"]["count"], 2)


if __name__ == "__main__":
    unittest.main()
