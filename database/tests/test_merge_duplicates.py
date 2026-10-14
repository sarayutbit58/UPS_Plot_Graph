"""Synthetic checks for conservative catalog identity reconciliation."""

import sqlite3
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from merge_duplicates import apply_merges, plan_merges


class MergeDuplicateTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:', isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        schema = Path(__file__).resolve().parents[1] / 'schema.sql'
        self.connection.executescript(schema.read_text(encoding='utf-8'))
        self.connection.executescript((schema.parent / 'seeds/001_reference.sql').read_text(encoding='utf-8'))
        self.connection.execute("INSERT INTO manufacturers(name,canonical_key) VALUES ('Test','test')")
        self.manufacturer_id = self.connection.execute('SELECT id FROM manufacturers').fetchone()[0]
        self.connection.execute("INSERT INTO source_files(relative_path,extension,size_bytes,status,scanned_at) VALUES ('test.csv','.csv',1,'imported','2026-01-01')")
        self.source_id = self.connection.execute('SELECT id FROM source_files').fetchone()[0]

    def tearDown(self):
        self.connection.close()

    def product(self, model, sku=None, rating=1000, topology=None, locator=None):
        key = f'{model}|{sku or ""}'
        product_id = self.connection.execute(
            'INSERT INTO products(manufacturer_id,identity_key,model,sku,product_type) VALUES (?,?,?,?,?)',
            (self.manufacturer_id, key, model, sku, 'UPS')).lastrowid
        self.connection.execute(
            'INSERT INTO product_sources(product_id,source_file_id,source_locator,raw_text,raw_json) VALUES (?,?,?,?,?)',
            (product_id, self.source_id, locator or f'row:{product_id}', model, '{}'))
        source_id = self.connection.execute('SELECT last_insert_rowid()').fetchone()[0]
        self.connection.execute(
            'INSERT INTO product_specifications(product_source_id,rating_va,topology) VALUES (?,?,?)', (source_id, rating, topology))
        return product_id

    def test_merges_cosmetic_model_variants_into_unique_sku_and_keeps_sources(self):
        loose = self.product('BU650E AS', locator='row:loose')
        canonical = self.product('BU650E-AS', 'CBP-BU650E', locator='row:canonical')
        plan = plan_merges(self.connection, minimum_score=0.80)
        self.assertEqual([(loose, canonical)], [(item['source_id'], item['target_id']) for item in plan['merges']])
        self.assertGreater(plan['merges'][0]['rule_score'], 0.80)

        result = apply_merges(self.connection, plan)
        self.assertEqual(1, result['products_removed'])
        self.assertEqual(2, self.connection.execute('SELECT count(*) FROM product_sources WHERE product_id=?', (canonical,)).fetchone()[0])
        self.assertEqual(1, self.connection.execute('SELECT count(*) FROM products').fetchone()[0])

    def test_does_not_merge_when_model_maps_to_multiple_skus(self):
        loose = self.product('3000VA/2700Watt', locator='row:loose')
        first = self.product('3000VA/2700Watt', 'APC-SRV3KI-E', locator='row:first')
        self.product('3000VA/2700Watt', 'APC-SRV3KRI-E', locator='row:second')
        plan = plan_merges(self.connection, minimum_score=0.80)
        self.assertEqual([], plan['merges'])
        self.assertIn(loose, [product_id for item in plan['skipped'] for product_id in item.get('product_ids', [])])
        self.assertNotIn(first, [item['source_id'] for item in plan['merges']])

    def test_does_not_merge_when_specifications_conflict(self):
        loose = self.product('BK650-AS', locator='row:loose', rating=650)
        canonical = self.product('BK650AS', 'BK650-AS', locator='row:canonical', rating=850)
        plan = plan_merges(self.connection, minimum_score=0.80)
        self.assertEqual([], plan['merges'])
        self.assertTrue(any(loose in item.get('product_ids', []) and canonical in item.get('product_ids', []) for item in plan['skipped']))

    def test_merges_model_only_duplicates_when_group_has_no_sku(self):
        first = self.product('3000VA/2700Watt', locator='row:first')
        second = self.product('3000VA/2700watt', locator='row:second')
        plan = plan_merges(self.connection, minimum_score=0.80)
        self.assertEqual([(second, first)], [(item['source_id'], item['target_id']) for item in plan['merges']])

    def test_does_not_merge_any_member_of_a_multi_sku_group(self):
        first = self.product('3000VA/2700Watt', locator='row:first')
        second = self.product('3000VA/2700watt', locator='row:second')
        self.product('3000VA/2700Watt', 'SKU-ONE', locator='row:sku1')
        self.product('3000VA/2700Watt', 'SKU-TWO', locator='row:sku2')
        plan = plan_merges(self.connection, minimum_score=0.80)
        self.assertEqual([], plan['merges'])
        self.assertTrue(any(first in item.get('product_ids', []) and second in item.get('product_ids', [])
                            for item in plan['skipped']))

    def test_skus_differing_only_by_case_remain_ambiguous(self):
        loose = self.product('MODEL-1000', locator='row:loose')
        self.product('MODEL1000', 'SKU-ONE', locator='row:sku1')
        self.product('MODEL-1000', 'sku-one', locator='row:sku2')
        plan = plan_merges(self.connection, minimum_score=0.80)
        self.assertEqual([], plan['merges'])
        self.assertTrue(any(loose in item.get('product_ids', []) for item in plan['skipped']))

    def test_does_not_merge_when_source_locators_collide(self):
        first = self.product('BK650-AS', locator='row:1')
        second = self.product('BK650AS', 'BK650-AS', locator='row:1')
        plan = plan_merges(self.connection, minimum_score=0.80)
        self.assertEqual([], plan['merges'])
        self.assertTrue(any(item['reason'] == 'same source file and locator on both products'
                            for item in plan['skipped']))

    def test_checks_spec_conflicts_across_the_whole_candidate_cluster(self):
        line_interactive = self.product('BK650AS', locator='row:line', topology='line-interactive')
        offline = self.product('BK650-AS', locator='row:offline', topology='offline')
        canonical = self.product('BK650-AS', 'BK650-AS', locator='row:canonical')
        plan = plan_merges(self.connection, minimum_score=0.80)
        self.assertEqual([(line_interactive, canonical)],
                         [(item['source_id'], item['target_id']) for item in plan['merges']])
        self.assertTrue(any(offline in item.get('product_ids', []) and 'topology' in item['reason']
                            for item in plan['skipped']))

    def test_rejects_candidate_when_target_has_internal_spec_conflicts(self):
        canonical = self.product('UPS-1000', 'SKU-1000', rating=650, locator='row:canonical')
        self.connection.execute("INSERT INTO product_sources(product_id,source_file_id,source_locator,raw_text,raw_json) VALUES (?,?,?,?,?)",
                                (canonical, self.source_id, 'row:canonical-2', 'conflicting assertion', '{}'))
        source_id = self.connection.execute('SELECT last_insert_rowid()').fetchone()[0]
        self.connection.execute('INSERT INTO product_specifications(product_source_id,rating_va) VALUES (?,?)', (source_id, 850))
        model_only = self.product('UPS1000', rating=650, locator='row:loose')
        plan = plan_merges(self.connection, minimum_score=0.80)
        self.assertEqual([], plan['merges'])
        self.assertTrue(any(model_only in item.get('product_ids', []) and 'rating_va' in item['reason']
                            for item in plan['skipped']))

    def test_apply_replans_atomically_if_a_competing_sku_appears(self):
        loose = self.product('MODEL-1000', locator='row:loose')
        target = self.product('MODEL1000', 'SKU-ONE', locator='row:sku1')
        plan = plan_merges(self.connection, minimum_score=0.80)
        self.assertEqual(1, len(plan['merges']))
        self.product('MODEL1000', 'SKU-TWO', locator='row:sku2')
        with self.assertRaisesRegex(ValueError, 'Catalog changed since planning'):
            apply_merges(self.connection, plan)
        self.assertIsNotNone(self.connection.execute('SELECT 1 FROM products WHERE id=?', (loose,)).fetchone())
        self.assertIsNotNone(self.connection.execute('SELECT 1 FROM products WHERE id=?', (target,)).fetchone())
        self.assertEqual(3, self.connection.execute('SELECT count(*) FROM products').fetchone()[0])

    def test_does_not_merge_distinct_skus_with_similar_models(self):
        first = self.product('9SX1000I', 'ETN-9SX1000I', locator='row:first')
        second = self.product('9SX1000IR', 'ETN-9SX1000IR', locator='row:second')
        plan = plan_merges(self.connection, minimum_score=0.80)
        self.assertEqual([], plan['merges'])
        self.assertEqual(2, self.connection.execute('SELECT count(*) FROM products').fetchone()[0])


if __name__ == '__main__':
    unittest.main()
