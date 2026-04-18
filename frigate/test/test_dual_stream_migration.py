"""Unit tests for migration 036 (stream_quality column + index).

Uses `peewee_migrate.Router` against a fresh temp-file SQLite DB to apply
migrations up to 036 and then inspect the resulting schema.
"""

import os
import tempfile
import unittest

from peewee import SqliteDatabase
from peewee_migrate import Router

# Path to the migrations directory in the repo.
MIGRATIONS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "migrations")
)


class TestMigration036(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = SqliteDatabase(self.db_path)
        self.db.connect()
        self.router = Router(self.db, migrate_dir=MIGRATIONS_DIR)

    def tearDown(self):
        try:
            self.db.close()
        finally:
            if os.path.exists(self.db_path):
                os.remove(self.db_path)

    def _run_through_035(self):
        """Apply every migration strictly before 036."""
        for name in sorted(self.router.todo):
            if name.startswith("036_"):
                break
            self.router.run_one(name, self.router.migrator, fake=False)

    def _run_036(self):
        for name in sorted(self.router.todo):
            if name.startswith("036_"):
                self.router.run_one(name, self.router.migrator, fake=False)
                return
        self.fail("migration 036 not found in router.todo")

    def _table_info(self, table):
        cur = self.db.execute_sql(f'PRAGMA table_info("{table}")').fetchall()
        # row: (cid, name, type, notnull, dflt_value, pk)
        return {row[1]: row for row in cur}

    def test_migration_036_adds_column(self):
        """After 036, recordings has `stream_quality VARCHAR(10) NOT NULL DEFAULT 'sub'`."""
        self._run_through_035()
        # sanity: column absent before
        info_before = self._table_info("recordings")
        self.assertNotIn("stream_quality", info_before)

        self._run_036()

        info = self._table_info("recordings")
        self.assertIn("stream_quality", info)
        _, _, col_type, notnull, dflt, _ = info["stream_quality"]
        self.assertIn("VARCHAR", col_type.upper())
        self.assertEqual(notnull, 1)
        # SQLite returns the default quoted.
        self.assertIn("sub", str(dflt))

    def test_migration_036_backfills_existing_rows_to_sub(self):
        """Existing rows get stream_quality='sub' via the NOT NULL DEFAULT."""
        self._run_through_035()

        # insert a handful of pre-036 rows
        for i in range(3):
            self.db.execute_sql(
                "INSERT INTO recordings "
                "(id, camera, path, start_time, end_time, duration, motion, objects, dBFS, segment_size, regions) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"id-{i}",
                    "back",
                    f"/tmp/rec-{i}.mp4",
                    1000.0 + i,
                    1010.0 + i,
                    10.0,
                    0,
                    0,
                    0,
                    0.0,
                    0,
                ),
            )

        self._run_036()

        cur = self.db.execute_sql(
            "SELECT DISTINCT stream_quality FROM recordings"
        ).fetchall()
        values = {row[0] for row in cur}
        self.assertEqual(values, {"sub"})

    def test_migration_036_creates_composite_index(self):
        """Index `recordings_stream_quality` exists after 036."""
        self._run_through_035()
        self._run_036()
        cur = self.db.execute_sql(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='recordings'"
        ).fetchall()
        names = {row[0] for row in cur}
        self.assertIn("recordings_stream_quality", names)

    def test_migration_036_rollback_is_noop(self):
        """rollback() does not raise and leaves the column intact."""
        self._run_through_035()
        self._run_036()

        from importlib import util as import_util

        module_path = os.path.join(MIGRATIONS_DIR, "036_add_stream_quality.py")
        spec = import_util.spec_from_file_location("mig036", module_path)
        mod = import_util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # Should not raise.
        result = mod.rollback(self.router.migrator, self.db)
        self.assertIsNone(result)

        info = self._table_info("recordings")
        self.assertIn("stream_quality", info)


if __name__ == "__main__":
    unittest.main()
