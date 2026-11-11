import re
import unittest
from datetime import date
from unittest.mock import patch

from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader, select_autoescape

import app as application


class TagDetailTimeTests(unittest.TestCase):
    def test_query_and_render_task_times(self):
        rows = [
            {"id": 1, "title": "Con hora", "due_date": date(2026, 11, 11), "due_time_text": "14:30"},
            {"id": 2, "title": "Medianoche", "due_date": date(2026, 11, 12), "due_time_text": "00:00"},
            {"id": 3, "title": "Sin hora", "due_date": date(2026, 11, 13), "due_time_text": None},
            {"id": 4, "title": "Sin fecha", "due_date": None, "due_time_text": None},
        ]
        with application.app.test_request_context("/tags/2"), \
             patch.object(application, "q1", side_effect=[{"id": 2, "name": "agenda"}, {"c": len(rows)}]), \
             patch.object(application, "q", return_value=rows) as query, \
             patch.object(application, "cfg_int", return_value=25), \
             patch.object(application, "load_config", return_value={}), \
             patch.object(application, "load_tags_map", return_value={}), \
             patch.object(application, "load_subtask_counts", return_value={}), \
             patch.object(application, "load_subtasks_map", return_value={}), \
             patch.object(application, "render_template", side_effect=lambda name, **context: context):
            context = application.tag_detail(2)
            self.assertIn("TIME_FORMAT(t.due_time, '%%H:%%i') AS due_time_text", query.call_args.args[0])

            env = Environment(
                loader=ChoiceLoader([
                    DictLoader({"base.html": "{% block content %}{% endblock %}"}),
                    FileSystemLoader(application.app.template_folder),
                ]),
                autoescape=select_autoescape(["html"]),
            )
            env.globals.update(
                url_for=lambda endpoint, **kwargs: "/",
                reminder_badge=lambda task_id: "",
                request=application.request,
            )
            html = env.get_template("tag_detail.html").render(**context)

        cells = re.findall(r'<td style="font-size: 0\.9rem;">(.*?)</td>', html, re.DOTALL)
        self.assertEqual(len(cells), 4)
        self.assertRegex(cells[0], r"11-11-2026\s*<div[^>]*>14:30</div>")
        self.assertRegex(cells[1], r"12-11-2026\s*<div[^>]*>00:00</div>")
        self.assertNotIn("<div", cells[2])
        self.assertIn("—", cells[3])
        self.assertNotIn("<div", cells[3])


if __name__ == "__main__":
    unittest.main()
