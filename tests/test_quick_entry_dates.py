import unittest
from datetime import date
from unittest.mock import patch

import app as application


class QuickEntryDateTests(unittest.TestCase):
    def test_numeric_dates_accept_slashes_with_or_without_star(self):
        with patch.object(application, "_today_madrid", return_value=date(2026, 1, 1)):
            for entry in ("Tarea *22/10", "Tarea 22/10"):
                with self.subTest(entry=entry):
                    due_date, title = application.extract_due_date_from_quick(entry)
                    self.assertEqual(due_date, date(2026, 10, 22))
                    self.assertEqual(title, "Tarea")

    def test_numeric_dates_accept_slash_year(self):
        with patch.object(application, "_today_madrid", return_value=date(2026, 1, 1)):
            due_date, title = application.extract_due_date_from_quick("Tarea *22/10/2027")

        self.assertEqual(due_date, date(2027, 10, 22))
        self.assertEqual(title, "Tarea")

    def test_numeric_date_does_not_mix_separators(self):
        with self.assertRaises(ValueError):
            application.parse_due_token("22-10/2027")


if __name__ == "__main__":
    unittest.main()
