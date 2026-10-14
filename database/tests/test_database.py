"""Database acceptance checks; all data fixtures are synthetic, never real UPS quotes."""

from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import manage


class DatabaseAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "synthetic.sqlite3"
        self.connection = manage.connect(self.db_path)

    def tearDown(self):
        self.connection.close()
        self.temporary.cleanup()

    def tables(self):
        return {row[0] for row in self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )}

    def prepare(self):
        manage.migrate(self.connection)
        manage.seed(self.connection)

    def test_migrate_seed_are_repeatable_and_seed_is_reference_only(self):
        self.assertTrue(manage.migrate(self.connection))
        self.assertFalse(manage.migrate(self.connection))
        manage.seed(self.connection)
        manage.seed(self.connection)
        self.assertEqual(1, self.connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0])
        for table in ("topology_types", "form_factors", "currencies"):
            self.assertEqual(3, self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in ("manufacturers", "products", "source_files", "product_sources", "product_specifications", "price_offers", "import_rejections"):
            self.assertEqual(0, self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        self.assertEqual([], self.connection.execute("PRAGMA foreign_key_check").fetchall())

    def test_rollback_then_rebuild_and_seed(self):
        self.prepare()
        self.assertTrue(manage.rollback(self.connection))
        self.assertEqual({"schema_migrations"}, self.tables())
        self.assertFalse(manage.rollback(self.connection))
        self.assertTrue(manage.migrate(self.connection))
        manage.seed(self.connection)
        self.assertEqual(3, self.connection.execute("SELECT COUNT(*) FROM currencies").fetchone()[0])

    def test_failed_migration_leaves_no_partial_schema(self):
        failing = Path(self.temporary.name) / "failure_migrations"
        failing.mkdir()
        (failing / "001_initial.up.sql").write_text(
            "CREATE TABLE synthetic_partial(id INTEGER); INSERT INTO absent_table VALUES(1);",
            encoding="utf-8",
        )
        with patch.object(manage, "MIGRATIONS_DIR", failing):
            with self.assertRaises(sqlite3.OperationalError):
                manage.migrate(self.connection)
        self.assertEqual(set(), self.tables())
        self.assertFalse(self.connection.in_transaction)

    def test_failed_rollback_restores_schema_and_metadata(self):
        self.prepare()
        original_tables = self.tables()
        failing = Path(self.temporary.name) / "failure_rollback"
        failing.mkdir()
        (failing / "001_initial.down.sql").write_text(
            "DROP TABLE currencies; INSERT INTO absent_table VALUES(1);", encoding="utf-8"
        )
        with patch.object(manage, "MIGRATIONS_DIR", failing):
            with self.assertRaises(sqlite3.OperationalError):
                manage.rollback(self.connection)
        self.assertEqual(original_tables, self.tables())
        self.assertEqual(1, manage.applied_version(self.connection))
        self.assertEqual(3, self.connection.execute("SELECT COUNT(*) FROM currencies").fetchone()[0])

    def test_failed_seed_leaves_no_partial_rows(self):
        manage.migrate(self.connection)
        failing = Path(self.temporary.name) / "failure_seeds"
        failing.mkdir()
        (failing / "001_reference.sql").write_text(
            "INSERT INTO currencies VALUES('ZZZ', 'SYNTHETIC'); INSERT INTO absent_table VALUES(1);",
            encoding="utf-8",
        )
        with patch.object(manage, "SEEDS_DIR", failing):
            with self.assertRaises(sqlite3.OperationalError):
                manage.seed(self.connection)
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM currencies").fetchone()[0])

    def test_every_connection_enforces_foreign_keys(self):
        self.prepare()
        second = manage.connect(self.db_path)
        try:
            self.assertEqual(1, second.execute("PRAGMA foreign_keys").fetchone()[0])
            with self.assertRaises(sqlite3.IntegrityError):
                second.execute("INSERT INTO product_specifications(product_source_id) VALUES (999999)")
            with self.assertRaises(sqlite3.IntegrityError):
                second.execute("INSERT INTO price_offers(product_source_id, amount_decimal) VALUES (999999, '1.00')")
        finally:
            second.close()

    def test_seed_requires_migration(self):
        with self.assertRaisesRegex(ValueError, "migrate"):
            manage.seed(self.connection)
        self.assertEqual(set(), self.tables())

    def test_provenance_identity_and_value_constraints(self):
        self.prepare()
        self.connection.execute(
            "INSERT INTO source_files(id, relative_path, sha256, extension, size_bytes, status, scanned_at) "
            "VALUES(1, 'SYNTHETIC_ONLY.csv', ?, '.csv', 0, 'imported', 'synthetic-time')",
            ('0' * 64,),
        )
        self.connection.execute(
            "INSERT INTO products(id, identity_key, model, product_type) "
            "VALUES(1, 'synthetic:test:ups', 'SYNTHETIC TEST UPS', 'UPS')"
        )
        provenance = "INSERT INTO product_sources(product_id, source_file_id, source_locator, raw_text, raw_json) VALUES(1, ?, ?, 'SYNTHETIC', '{}')"
        for source_id, locator in ((999999, 'row:1'), (1, '')):
            with self.assertRaises(sqlite3.IntegrityError):
                self.connection.execute(provenance, (source_id, locator))
        self.connection.execute(provenance, (1, 'row:1'))
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(provenance, (1, 'row:1'))
        for amount in ('-1', 'not-a-price', '1.2.3'):
            with self.assertRaises(sqlite3.IntegrityError):
                self.connection.execute("INSERT INTO price_offers(product_source_id, amount_decimal) VALUES(1, ?)", (amount,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("INSERT INTO product_specifications(product_source_id, rating_va) VALUES(1, -1)")
        self.connection.execute("INSERT INTO price_offers(product_source_id, amount_decimal, currency) VALUES(1, '1.00', 'THB')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM source_files WHERE id = 1")
        self.assertEqual([], self.connection.execute("PRAGMA foreign_key_check").fetchall())
        self.assertTrue(manage.rollback(self.connection))

    def test_cli_actions(self):
        for command in ("migrate", "migrate", "seed", "seed", "rollback", "rollback", "migrate", "seed"):
            self.assertEqual(0, manage.main([command, "--db", str(self.db_path)]))


if __name__ == "__main__":
    unittest.main()
