"""Tests del sistema de migraciones (gtd/schema.py), sin base de datos."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app  # noqa: F401
from gtd import schema
from tests.support import patch_all


class DiscoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, name, body="def upgrade(ctx):\n    ctx.exec_sql('SELECT 1')\n"):
        (self.dir / name).write_text(body, encoding="utf-8")

    def test_sorted_and_ignores_other_files(self):
        self.write("0002_second.py")
        self.write("0001_first.py")
        self.write("README.md")
        self.write("helper.py")
        self.write("0003_Mal_Nombre.py")
        found = schema.discover_migrations(self.dir)
        self.assertEqual([m.label for m in found], ["0001_first", "0002_second"])

    def test_duplicate_versions_fail(self):
        self.write("0001_a.py")
        self.write("0001_b.py")
        with self.assertRaises(RuntimeError):
            schema.discover_migrations(self.dir)

    def test_pending(self):
        self.write("0001_a.py")
        self.write("0002_b.py")
        self.write("0003_c.py")
        pending = schema.pending_migrations(schema.discover_migrations(self.dir), ["0001", "0003"])
        self.assertEqual([m.version for m in pending], ["0002"])

    def test_next_migration_path(self):
        self.assertEqual(schema.next_migration_path("Primera", self.dir).name, "0001_primera.py")
        self.write("0007_x.py")
        self.assertEqual(schema.next_migration_path("Añadir índice a tareas", self.dir).name,
                         "0008_anadir_indice_a_tareas.py")
        with self.assertRaises(ValueError):
            schema.next_migration_path("¡¡!!", self.dir)

    def test_missing_upgrade_fails_on_load(self):
        self.write("0001_bad.py", "X = 1\n")
        with self.assertRaises(RuntimeError):
            schema.discover_migrations(self.dir)[0].load()


class RealMigrationsTests(unittest.TestCase):
    def test_repository_migrations_load(self):
        migrations = schema.discover_migrations()
        self.assertGreaterEqual(len(migrations), 2)
        self.assertEqual(migrations[0].label, "0001_baseline")
        for migration in migrations:
            with self.subTest(migration=migration.label):
                self.assertTrue(callable(migration.load().upgrade))


class ApplyTests(unittest.TestCase):
    """apply_migrations con la BD simulada: orden, registro y bloqueo."""

    def run_apply(self, applied, fail_on=None):
        calls = []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        for version in ("0001", "0002", "0003"):
            boom = "raise RuntimeError('boom')" if version == fail_on else "pass"
            (directory / f"{version}_m.py").write_text(
                f"def upgrade(ctx):\n    ctx.exec_sql('UPGRADE {version}')\n    {boom}\n", encoding="utf-8")

        def exec_sql(sql, params=()):
            calls.append((sql.split()[0] if not sql.startswith("UPGRADE") else sql, params))
            return 0

        def q(sql, params=()):
            return [{"version": v} for v in applied]

        def q1(sql, params=()):
            calls.append(("LOCK" if "GET_LOCK" in sql else "UNLOCK", params))
            return {"ok": 1}

        with patch.object(schema, "MIGRATIONS_DIR", directory), \
             patch_all("exec_sql", exec_sql), patch_all("q", q), patch_all("q1", q1), \
             patch_all("commit") as commit, patch_all("rollback") as rollback:
            try:
                result = schema.apply_migrations()
            except RuntimeError as exc:
                result = exc
        return result, calls, commit, rollback

    def test_applies_only_pending_in_order_and_records_them(self):
        result, calls, commit, _ = self.run_apply(applied=["0001"])
        self.assertEqual(result, ["0002_m", "0003_m"])
        upgrades = [c[0] for c in calls if c[0].startswith("UPGRADE")]
        self.assertEqual(upgrades, ["UPGRADE 0002", "UPGRADE 0003"])
        inserts = [c[1] for c in calls if c[0] == "INSERT"]
        self.assertEqual(inserts, [("0002", "m"), ("0003", "m")])
        self.assertEqual(commit.call_count, 2)
        self.assertEqual(calls[0][0], "LOCK")
        self.assertEqual(calls[-1][0], "UNLOCK")

    def test_failure_stops_rolls_back_and_releases_lock(self):
        result, calls, _, rollback = self.run_apply(applied=[], fail_on="0002")
        self.assertIsInstance(result, RuntimeError)
        inserts = [c[1] for c in calls if c[0] == "INSERT"]
        self.assertEqual(inserts, [("0001", "m")])
        self.assertNotIn("UPGRADE 0003", [c[0] for c in calls])
        rollback.assert_called_once()
        self.assertEqual(calls[-1][0], "UNLOCK")


if __name__ == "__main__":
    unittest.main()
