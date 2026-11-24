"""Tests del parser de entrada rápida (gtd/quick_entry.py).

Sintaxis: ``Tarea @etiqueta #proyecto f:carpeta *fecha HH:MM ^prioridad cada …``.
``parse_quick_entry`` es la cadena completa que usa el Inbox (/tasks/create);
``parse_task_quick_entry`` es la que usa /tasks/quick_add tras extraer fecha/hora.
"""
import unittest
from datetime import date, time
from unittest.mock import patch

import app  # noqa: F401  inicializa la app (roles, config)
from gtd import quick_entry as qe

# Lunes 23-11-2026
TODAY = date(2026, 11, 23)


class QuickEntryTestCase(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(qe, "_today_madrid", return_value=TODAY)
        patcher.start()
        self.addCleanup(patcher.stop)

    def parse(self, text, **kwargs):
        return qe.parse_quick_entry(text, **kwargs)


class FullEntryTests(QuickEntryTestCase):
    def test_plain_title(self):
        self.assertEqual(self.parse("Comprar pan"), qe.QuickEntry(title="Comprar pan"))

    def test_all_tokens_together(self):
        entry = self.parse("Llamar a Juan @NextAction @casa #Reforma f:Trabajo *mañana 9:15 ^alta")
        self.assertEqual(entry, qe.QuickEntry(
            title="Llamar a Juan", tags=["NextAction", "casa"], project="Reforma", folder="Trabajo",
            due_date=date(2026, 11, 24), due_time=time(9, 15), priority=1,
        ))

    def test_tokens_in_any_position(self):
        entry = self.parse("^baja #Casa Pintar @finde la valla *+2")
        self.assertEqual((entry.title, entry.tags, entry.project, entry.priority, entry.due_date),
                         ("Pintar la valla", ["finde"], "Casa", 3, date(2026, 11, 25)))

    def test_quoted_project_and_folder_names_with_spaces(self):
        entry = self.parse('Revisar #"Proyecto con espacios" f:"Mi carpeta"')
        self.assertEqual((entry.title, entry.project, entry.folder),
                         ("Revisar", "Proyecto con espacios", "Mi carpeta"))

    def test_quoted_text_is_literal(self):
        entry = self.parse('Escribir a "marcos@udc.es" a las "20:00" "#nota"')
        self.assertEqual(entry, qe.QuickEntry(title="Escribir a marcos@udc.es a las 20:00 #nota"))

    def test_trailing_separators_are_trimmed_from_title(self):
        self.assertEqual(self.parse("Comprar pan - @casa").title, "Comprar pan")

    def test_empty_title_when_only_tokens(self):
        self.assertEqual(self.parse("@casa #Proyecto").title, "")

    def test_date_like_project_is_ignored(self):
        entry = self.parse("Reunión #12-12-2026")
        self.assertEqual((entry.title, entry.project, entry.due_date), ("Reunión", None, None))

    def test_form_values_disable_detection(self):
        entry = self.parse("Tarea *hoy 10:00", parse_date=False, parse_time=False)
        self.assertEqual((entry.title, entry.due_date, entry.due_time), ("Tarea *hoy 10:00", None, None))

    def test_invalid_date_raises(self):
        with self.assertRaises(ValueError):
            self.parse("Tarea *31-02")

    def test_invalid_time_raises(self):
        with self.assertRaises(ValueError):
            self.parse("Tarea 25:00")

    @unittest.expectedFailure  # Bug conocido: el fragmento de la URL se toma como #proyecto
    def test_url_fragment_is_not_a_project(self):
        entry = self.parse("Leer https://example.com/a#seccion")
        self.assertEqual((entry.title, entry.project), ("Leer https://example.com/a#seccion", None))

    @unittest.expectedFailure  # Bug conocido: la puntuación queda dentro de la etiqueta ("casa,")
    def test_tag_does_not_include_trailing_punctuation(self):
        self.assertEqual(self.parse("Comprar @casa, pan").tags, ["casa"])


class DueDateTests(QuickEntryTestCase):
    def due(self, text):
        return qe.extract_due_date_from_quick(text)

    def test_relative_tokens(self):
        cases = {"*hoy": date(2026, 11, 23), "*mañana": date(2026, 11, 24), "*+10": date(2026, 12, 3)}
        for token, expected in cases.items():
            with self.subTest(token=token):
                self.assertEqual(self.due(f"Tarea {token}"), (expected, "Tarea"))

    def test_weekday_is_always_in_the_future(self):
        # Hoy es lunes: *lunes es el de la semana siguiente
        self.assertEqual(self.due("Tarea *lunes")[0], date(2026, 11, 30))
        self.assertEqual(self.due("Tarea *miércoles")[0], date(2026, 11, 25))
        self.assertEqual(self.due("Tarea *miercoles")[0], date(2026, 11, 25))
        self.assertEqual(self.due("Tarea *domingo")[0], date(2026, 11, 29))

    def test_numeric_dates(self):
        cases = {"*25-12": date(2026, 12, 25), "*25/12/2027": date(2027, 12, 25), "25-12": date(2026, 12, 25)}
        for token, expected in cases.items():
            with self.subTest(token=token):
                self.assertEqual(self.due(f"Cena {token}"), (expected, "Cena"))

    def test_star_token_wins_over_bare_date(self):
        self.assertEqual(self.due("Entregar 3/4 *hoy"), (TODAY, "Entregar 3/4"))

    def test_bare_keywords_are_dates(self):
        self.assertEqual(self.due("Pasar a recoger mañana el paquete"),
                         (date(2026, 11, 24), "Pasar a recoger el paquete"))

    def test_no_date(self):
        self.assertEqual(self.due("Sin fecha"), (None, "Sin fecha"))


class TimePriorityRecurrenceTests(QuickEntryTestCase):
    def test_time_formats(self):
        for text in ("Tarea 9:15", "Tarea h:9:15", "Tarea 09:15"):
            with self.subTest(text=text):
                self.assertEqual(qe.extract_due_time_from_quick(text), (time(9, 15), "Tarea"))

    def test_time_inside_url_is_not_extracted(self):
        text = "Ver https://example.com/video?t=12:30"
        self.assertEqual(qe.extract_due_time_from_quick(text), (None, text))

    def test_priority(self):
        for token, expected in (("^alta", 1), ("^MEDIA", 2), ("^baja", 3)):
            with self.subTest(token=token):
                self.assertEqual(qe.extract_priority_from_quick(f"Tarea {token}"), (expected, "Tarea"))
        self.assertEqual(qe.extract_priority_from_quick("Tarea ^urgente"), (None, "Tarea ^urgente"))

    def test_recurrence(self):
        cases = {
            "cada día": "FREQ=DAILY;INTERVAL=1",
            "cada dia": "FREQ=DAILY;INTERVAL=1",
            "cada semana": "FREQ=WEEKLY;INTERVAL=1",
            "cada mes": "FREQ=MONTHLY;INTERVAL=1",
            "cada año": "FREQ=YEARLY;INTERVAL=1",
            "cada laboral": "FREQ=WORKDAY;INTERVAL=1",
        }
        for token, rule in cases.items():
            with self.subTest(token=token):
                self.assertEqual(qe.extract_recurrence_from_quick(f"Regar {token}"), (rule, "Regar"))

    def test_form_recurrence_takes_precedence_but_token_is_removed(self):
        self.assertEqual(qe.extract_recurrence_from_quick("Regar cada semana", "FREQ=DAILY;INTERVAL=1"),
                         ("FREQ=DAILY;INTERVAL=1", "Regar"))

    def test_quoted_recurrence_is_literal(self):
        self.assertEqual(qe.extract_recurrence_from_quick('Leer "cada semana"'), (None, 'Leer "cada semana"'))


class QuickAddAndTagsCsvTests(QuickEntryTestCase):
    def test_parse_task_quick_entry(self):
        self.assertEqual(qe.parse_task_quick_entry('Llamar @NextAction #"Mi proyecto" f:Casa'),
                         ("Llamar", ["NextAction"], "Mi proyecto", "Casa"))

    def test_parse_task_quick_entry_stops_tags_at_punctuation(self):
        # /tasks/quick_add usa TAG_RE (más estricta que la del Inbox)
        self.assertEqual(qe.parse_task_quick_entry("Comprar @casa, pan")[1], ["casa"])

    def test_parse_tags_csv(self):
        self.assertEqual(qe.parse_tags_csv("@NextAction, @Casa"), ["NextAction", "Casa"])
        self.assertEqual(qe.parse_tags_csv("next casa"), ["next", "casa"])
        self.assertEqual(qe.parse_tags_csv("  "), [])


if __name__ == "__main__":
    unittest.main()
