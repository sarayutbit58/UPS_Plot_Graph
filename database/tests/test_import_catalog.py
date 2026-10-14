"""Importer acceptance checks using labelled synthetic JSONL fixtures only."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from import_catalog import import_catalog
from merge_duplicates import apply_merges, plan_merges
from manage import connect
from validate import validate


class ImportCatalogAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db = self.root / 'synthetic.sqlite3'
        self.inventory = self.root / 'synthetic-inventory.jsonl'
        self.records = self.root / 'synthetic-records.jsonl'

    def tearDown(self):
        self.temporary.cleanup()

    def source(self, path='SYNTHETIC/catalog.csv', digest='a' * 64, **updates):
        item = dict(relative_path=path, sha256=digest, extension='.csv',
                    size_bytes=100, status='imported', scanned_at='2026-01-01T00:00:00Z')
        item.update(updates)
        return item

    def record(self, path='SYNTHETIC/catalog.csv', locator='row:1', **updates):
        item = dict(source_path=path, source_locator=locator,
                    manufacturer='SYNTHETIC Vendor', model='SYNTHETIC UPS A',
                    manufacturer_sku='SYNTHETIC-SKU-A', product_type='UPS',
                    rating_va=1000, rating_w=600, topology='line-interactive',
                    form_factor='tower', price_amount='12345678901234567890.1200',
                    currency=None, raw_text='SYNTHETIC FIXTURE - not real product or price')
        item.update(updates)
        return item

    def write(self, inventory, records):
        for path, rows in ((self.inventory, inventory), (self.records, records)):
            path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows), encoding='utf-8')

    def run_import(self):
        return import_catalog(self.db, self.inventory, self.records)

    def snapshot(self):
        connection = connect(self.db)
        try:
            tables = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )]
            return {table: [tuple(row) for row in connection.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                    for table in tables}
        finally:
            connection.close()

    def test_fresh_import_and_validation_report(self):
        self.write([self.source()], [self.record()])
        totals = self.run_import()
        self.assertEqual(1, totals['accepted'])
        self.assertEqual(1, totals['products'])
        report_path = self.root / 'synthetic-validation.json'
        report = validate(self.db, report_path)
        self.assertTrue(report['passed'], report['errors'])
        self.assertEqual(['ok'], report['integrity'])
        self.assertEqual([], report['foreign_key_violations'])
        self.assertTrue(report['content_coverage_complete'])
        self.assertEqual(1, report['unknown_currency_offers'])
        self.assertTrue(report_path.is_file())
        self.assertTrue(report_path.with_suffix('.md').is_file())

    def test_repeat_import_deduplicates_and_retains_distinct_sources(self):
        self.write([self.source(), self.source('SYNTHETIC/second.csv', 'b' * 64)],
                   [self.record(), self.record('SYNTHETIC/second.csv')])
        totals = self.run_import()
        self.assertEqual(1, totals['products'])
        self.assertEqual(2, totals['product_sources'])
        before = self.snapshot()
        repeated = self.run_import()
        self.assertEqual(0, repeated['accepted'])
        self.assertEqual(2, repeated['duplicates'])
        self.assertEqual(before, self.snapshot())

    def test_model_only_import_merges_case_and_punctuation_variants(self):
        self.write([self.source()], [
            self.record(manufacturer_sku=None, model='U-650/AS', locator='row:1'),
            self.record(manufacturer_sku=None, model='u650 as', locator='row:2'),
        ])
        totals = self.run_import()
        self.assertEqual(1, totals['products'])
        self.assertEqual(2, totals['product_sources'])

    def test_import_remains_idempotent_after_model_only_rows_join_sku_products(self):
        self.write([self.source()], [
            self.record(manufacturer_sku=None, model='U-650/AS', locator='row:1'),
            self.record(model='U650 AS', manufacturer_sku='MAKER-650AS', locator='row:2'),
        ])
        self.run_import()
        connection = connect(self.db)
        try:
            plan = plan_merges(connection)
            self.assertEqual(1, len(plan['merges']))
            apply_merges(connection, plan)
        finally:
            connection.close()
        before = self.snapshot()
        repeated = self.run_import()
        self.assertEqual(2, repeated['duplicates'])
        self.assertEqual(before, self.snapshot())

    def test_same_identity_with_different_model_is_quarantined(self):
        self.write([self.source()], [self.record(), self.record(locator='row:2', model='SYNTHETIC UPS CONFLICT')])
        totals = self.run_import()
        self.assertEqual(1, totals['accepted'])
        self.assertEqual(1, totals['rejected'])
        self.assertEqual(1, totals['products'])
        self.assertEqual(1, totals['product_sources'])
        rows = self.snapshot()
        self.assertIn('identity conflict', rows['import_rejections'][0][3])
        self.assertIn('SYNTHETIC UPS CONFLICT', rows['import_rejections'][0][4])
        self.assertEqual('SYNTHETIC UPS A', rows['products'][0][3])

    def test_missing_inventory_source_rolls_back_every_import_row(self):
        self.write([self.source()], [self.record(), self.record('SYNTHETIC/missing.csv')])
        with self.assertRaisesRegex(ValueError, 'missing from inventory'):
            self.run_import()
        rows = self.snapshot()
        for table in ('source_files', 'manufacturers', 'products', 'product_sources',
                      'product_specifications', 'price_offers', 'import_rejections'):
            self.assertEqual([], rows[table], table)
        self.assertEqual(1, len(rows['schema_migrations']))
        self.assertEqual(3, len(rows['currencies']))

    def test_changed_source_hash_rejects_and_preserves_old_assertions(self):
        self.write([self.source()], [self.record()])
        self.run_import()
        before = self.snapshot()
        self.write([self.source(digest='c' * 64, size_bytes=500)],
                   [self.record(price_amount='999.99', rating_va=2000)])
        with self.assertRaisesRegex(ValueError, 'Source changed'):
            self.run_import()
        self.assertEqual(before, self.snapshot())

    def test_exact_decimal_and_unknown_currency_are_preserved(self):
        self.write([self.source()], [self.record()])
        self.run_import()
        connection = connect(self.db)
        try:
            row = connection.execute('SELECT amount_decimal,currency,vat_treatment FROM price_offers').fetchone()
            self.assertEqual('12345678901234567890.1200', row['amount_decimal'])
            self.assertIsNone(row['currency'])
            self.assertEqual('unknown', row['vat_treatment'])
            original = json.loads(connection.execute('SELECT raw_json FROM product_sources').fetchone()[0])
            self.assertEqual(self.record(), original)
        finally:
            connection.close()

    def test_unreadable_inventory_keeps_unknown_hash_and_coverage_issue(self):
        self.write([self.source(), self.source('SYNTHETIC/unreadable.pdf', None,
                                             status='error', error='SYNTHETIC read failure')], [self.record()])
        self.run_import()
        report = validate(self.db)
        self.assertTrue(report['passed'], report['errors'])
        self.assertFalse(report['content_coverage_complete'])
        self.assertEqual('SYNTHETIC/unreadable.pdf', report['source_issues'][0]['relative_path'])
        connection = connect(self.db)
        try:
            self.assertIsNone(connection.execute("SELECT sha256 FROM source_files WHERE relative_path='SYNTHETIC/unreadable.pdf'").fetchone()[0])
        finally:
            connection.close()


if __name__ == '__main__':
    unittest.main()
