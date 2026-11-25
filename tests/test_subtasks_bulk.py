"""Alta de subtareas en bloque: una por línea, con fecha opcional al final."""
import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import app as application
import subtasks
from subtasks import parse_subtask_lines

TODAY = date(2026, 11, 25)  # miércoles


class ParseSubtaskLinesTests(unittest.TestCase):
    def setUp(self):
        p = patch("gtd.quick_entry._today_madrid", return_value=TODAY)
        p.start()
        self.addCleanup(p.stop)

    def test_one_per_line_skipping_blank_lines(self):
        self.assertEqual(parse_subtask_lines("Uno\n\n  Dos  \r\n"), [("Uno", None), ("Dos", None)])

    def test_strips_bullets(self):
        text = "- Uno\n* Dos\n• Tres\n1. Cuatro\n2) Cinco\n- [ ] Seis\n[x] Siete"
        self.assertEqual([t for t, _ in parse_subtask_lines(text)],
                         ["Uno", "Dos", "Tres", "Cuatro", "Cinco", "Seis", "Siete"])

    def test_trailing_dates(self):
        items = parse_subtask_lines(
            "Notario *15-12\nPapeles 20/01/2027\nLlamar hoy\nEnviar mañana\nRevisar *viernes\nPagar *+3"
        )
        self.assertEqual(items, [
            ("Notario", date(2026, 12, 15)),
            ("Papeles", date(2027, 1, 20)),
            ("Llamar", TODAY),
            ("Enviar", TODAY + timedelta(days=1)),
            ("Revisar", date(2026, 11, 27)),
            ("Pagar", TODAY + timedelta(days=3)),
        ])

    def test_bare_weekday_or_inner_date_is_kept_in_title(self):
        self.assertEqual(parse_subtask_lines("Preparar la reunión del lunes"),
                         [("Preparar la reunión del lunes", None)])
        self.assertEqual(parse_subtask_lines("Cita del 15-12 con Ana"),
                         [("Cita del 15-12 con Ana", None)])

    def test_line_with_only_a_date_keeps_it_as_title(self):
        self.assertEqual(parse_subtask_lines("15-12"), [("15-12", None)])
        self.assertEqual(parse_subtask_lines("- *hoy"), [("*hoy", None)])

    def test_invalid_date_raises_with_line(self):
        with self.assertRaisesRegex(ValueError, "Notario"):
            parse_subtask_lines("Uno\nNotario 31-02")


class SubtaskAddRouteTests(unittest.TestCase):
    def _post(self, data):
        db = MagicMock()
        db.q1.return_value = {"id": 3}
        view = application.app.view_functions["subtask_add"]
        with application.app.test_request_context("/tasks/3/subtasks/add", method="POST", data=data), \
             patch.object(subtasks, "flash") as flash:
            # La ruta captura `db` en su closure: lo sustituimos temporalmente.
            cell = next(c for c in view.__closure__ if isinstance(c.cell_contents, subtasks.DB))
            original = cell.cell_contents
            cell.cell_contents = db
            try:
                view(task_id=3)
            finally:
                cell.cell_contents = original
        return db, flash

    def test_creates_one_row_per_line(self):
        db, flash = self._post({"title": "- Uno\n- Dos 15-12-2026\n", "due_date": "01-12-2026"})
        inserts = [c.args[1] for c in db.exec_sql.call_args_list]
        self.assertEqual(inserts, [
            (3, "Uno", None, date(2026, 12, 1)),
            (3, "Dos", None, date(2026, 12, 15)),
        ])
        db.commit.assert_called_once()
        self.assertIn("2 subtareas creadas", flash.call_args.args[0])

    def test_single_line_keeps_description(self):
        db, _ = self._post({"title": "Uno", "description": "Detalle"})
        self.assertEqual(db.exec_sql.call_args.args[1], (3, "Uno", "Detalle", None))

    def test_description_with_several_lines_is_rejected(self):
        db, flash = self._post({"title": "Uno\nDos", "description": "Detalle"})
        db.exec_sql.assert_not_called()
        self.assertEqual(flash.call_args.args[1], "error")

    def test_invalid_date_creates_nothing(self):
        db, flash = self._post({"title": "Uno\nDos 31-02"})
        db.exec_sql.assert_not_called()
        self.assertIn("No se ha creado ninguna", flash.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
