"""Prior evidence remains traceable without publishing local source paths."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from published_catalog import project_model


class ProjectionHistoryTests(unittest.TestCase):
    def test_merged_model_only_alias_is_recorded_as_unverified_bundle(self):
        product = {'id': 1, 'manufacturer': 'APC', 'model': 'UPS-1', 'sku': 'UPS-1',
                   'product_type': 'UPS', 'rating_w': 700, 'rating_va': 900, 'outlet_count': 4,
                   'warnings': ['Combined a model-only catalogue row with its exact manufacturer-SKU row'],
                   'evidence': [], 'prices': []}
        result = project_model(product)
        self.assertTrue(result.get('bundle_identity_unverified'))

    def test_previous_outlet_and_battery_sources_survive_redacted(self):
        product = {'id': 1, 'manufacturer': 'APC', 'model': 'UPS-1', 'sku': 'UPS-1',
                   'product_type': 'UPS', 'rating_w': 700, 'rating_va': 900,
                   'outlet_count': 4, 'warnings': [], 'evidence': [], 'prices': [],
                   'previous_outlet_groups': [{'type': 'IEC_C13', 'power': 'battery', 'count': 4,
                       'source': {'path': 'C:/private/catalog.xlsx', 'locator': 'row:1'}}],
                   'previous_battery_configurations': [{'id': 'internal', 'label': 'C:/private/label.txt',
                       'points': [{'loadW': 100, 'runtimeMinutes': 20}, {'loadW': 700, 'runtimeMinutes': 5}],
                       'includedPacks': [], 'extraPacks': [],
                       'source': {'path': 'C:/private/runtime.xlsx', 'locator': 'row:2'}}]}
        result = project_model(product, include_prices=True)
        self.assertEqual(1, len(result.get('previous_outlet_groups', [])))
        self.assertEqual(1, len(result.get('previous_battery_configurations', [])))
        self.assertTrue(result['previous_outlet_groups'][0]['source']['sourceId'])
        self.assertNotIn('C:/private', json.dumps(result))

    def test_history_labels_do_not_leak_windows_or_unc_paths(self):
        for label in (r'C:\Users\person\source.xlsx', r'\\server\share\source.xlsx',
                      'file://private/source.xlsx'):
            with self.subTest(label=label):
                item = {'id': 1, 'manufacturer': 'APC', 'model': 'UPS-1', 'sku': 'UPS-1',
                        'product_type': 'UPS', 'rating_w': 700, 'rating_va': 900,
                        'outlet_count': 4, 'warnings': [], 'evidence': [], 'prices': [],
                        'previous_battery_configurations': [{'id': 'internal', 'label': label,
                            'points': [], 'includedPacks': [], 'extraPacks': [], 'source': {}}]}
                result = project_model(item)
                self.assertNotIn(label, json.dumps(result))
                self.assertIsNone(result['previous_battery_configurations'][0]['label'])


if __name__ == '__main__':
    unittest.main()
