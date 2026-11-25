"""Formato de la hora de las tareas (filtro Jinja ``hhmm``).

pymysql devuelve las columnas TIME como ``timedelta``; antes se recortaba ``str()[:5]``
y las horas < 10 salían como "9:15:" (y el <input type="time"> de editar quedaba vacío).
"""
import unittest
from datetime import datetime, time, timedelta

import app as application
from gtd.utils import format_hhmm
from gtd.views import extension as extension_views


class FormatHhmmTests(unittest.TestCase):
    def test_values(self):
        cases = [
            (timedelta(hours=9, minutes=15), "09:15"),
            (timedelta(hours=10, minutes=30), "10:30"),
            (timedelta(0), "00:00"),
            (time(7, 5), "07:05"),
            (datetime(2026, 11, 30, 8, 0), "08:00"),
            ("9:15:00", "09:15"),
            ("23:59:59", "23:59"),
            (None, ""),
            ("", ""),
            ("sin hora", ""),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(format_hhmm(value), expected)

    def test_jinja_filter_for_time_input(self):
        template = application.app.jinja_env.from_string('<input type="time" value="{{ t|hhmm }}">')
        self.assertEqual(template.render(t=timedelta(hours=9, minutes=15)), '<input type="time" value="09:15">')
        self.assertEqual(template.render(t=None), '<input type="time" value="">')

    def test_extension_api_time(self):
        self.assertEqual(extension_views._to_hhmm(timedelta(hours=9, minutes=15)), "09:15")
        self.assertIsNone(extension_views._to_hhmm(None))


if __name__ == "__main__":
    unittest.main()
