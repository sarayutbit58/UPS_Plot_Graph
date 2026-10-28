"""Catalog controls preserve unknown facts and never expose commercial sources."""
import json
from pathlib import Path
import sqlite3
import sys
import unittest
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_recommendation_engine import add_model_only_alias, seed_catalog
from verified_fixture import verified_curve
from catalog_search import search_catalog_from_connection, search_catalog
from recommendation_engine import RecommendationInputError
from verified_evidence import MANUFACTURER_DOMAINS


class CatalogSearchTests(unittest.TestCase):
    def setUp(self):
        domains = patch.dict(MANUFACTURER_DOMAINS, {'test vendor': ('vendor.example',)})
        domains.start()
        self.addCleanup(domains.stop)
        self.connection = sqlite3.connect(':memory:', isolation_level=None)
        seed_catalog(self.connection)
        self.curves = [verified_curve(
            f'UPS-{index + 1}', 700 + 100 * index, 900 + 100 * index, 4 + index,
            [{'loadW': 100, 'runtimeMinutes': 30 + index},
             {'loadW': 700 + 100 * index, 'runtimeMinutes': 14 + index}],
        ) for index in range(6)]
        self.curves.append(verified_curve('UPS-TOO-SMALL', 440, 550, 4,
                                         [{'loadW': 100, 'runtimeMinutes': 30},
                                          {'loadW': 440, 'runtimeMinutes': 14}]))

    def tearDown(self):
        self.connection.close()

    def search(self, **updates):
        request = {'requiredWatts': 400, 'requiredVa': 500,
                   'requiredOutlets': 2, 'runtimeRequirementMinutes': 10}
        request.update(updates)
        return search_catalog_from_connection(self.connection, request, curve_catalog=self.curves)

    def test_exact_only_returns_required_dimensions_passing(self):
        result = self.search()
        self.assertGreater(result['total'], 0)
        for model in result['models']:
            self.assertEqual('matches', model['status'])
            self.assertTrue(all(c['status'] == 'pass' for c in model['checks'].values() if c['enabled']))

    def test_search_wrapper_closes_the_database_before_returning(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'catalog.sqlite3'
            path.touch()
            with patch('catalog_search.sqlite3.connect', return_value=self.connection):
                result = search_catalog(path, {}, curve_catalog=self.curves)
            self.assertGreater(result['total'], 0)
        with self.assertRaises(sqlite3.ProgrammingError):
            self.connection.execute('SELECT 1')

    def test_matching_models_prefer_adequate_capacity_fit_over_alphabetical_name(self):
        closest = self.connection.execute("SELECT id FROM products WHERE model='UPS-1' AND sku IS NOT NULL").fetchone()[0]
        self.connection.execute("UPDATE products SET model='Z-closest-fit' WHERE id=?", (closest,))
        self.connection.execute("UPDATE products SET model='A-oversized' WHERE model='UPS-6'")
        result = self.search(requireRuntime=False, requireOutlets=False)
        self.assertEqual(closest, result['models'][0]['productId'])

    def test_unknown_runtime_requires_explicit_nearby_inclusion(self):
        self.assertEqual(0, self.search(requiredWatts=50, requiredVa=62.5)['total'])
        self.assertEqual(0, self.search(requiredWatts=50, requiredVa=62.5, mode='nearby')['total'])
        result = self.search(requiredWatts=50, requiredVa=62.5, mode='nearby', includeUnknown=True)
        self.assertGreater(result['total'], 0)
        self.assertTrue(all(m['status'] == 'unknown' for m in result['models']))
        self.assertTrue(all(m['runtimeMinutes'] is None for m in result['models']))
        self.assertTrue(all(m['checks']['runtime']['reason'] == 'load_below_curve_range' for m in result['models']))

    def test_total_outlets_are_unknown_for_backup_filter_without_evidence(self):
        self.curves[0].pop('outletGroups')
        result = self.search(query='UPS-1', requireRuntime=False,
                             mode='nearby', includeUnknown=True)
        self.assertEqual(1, result['total'])
        model = result['models'][0]
        self.assertEqual('unknown', model['checks']['outlets']['status'])
        self.assertIsNone(model['checks']['outlets']['available'])
        self.assertFalse(model['canOptimize'])

    def test_nearby_labels_unverified_identity_even_when_filters_are_disabled(self):
        self.curves[0]['identityVerified'] = False
        result = self.search(query='UPS-1', mode='nearby', includeUnknown=True,
                             requireRuntime=False, requireOutlets=False)
        self.assertEqual(1, result['total'])
        model = result['models'][0]
        self.assertEqual('unknown', model['status'])
        self.assertFalse(model['identityVerified'])
        self.assertFalse(model['canOptimize'])

    def test_model_only_alias_is_visible_as_unknown_not_a_verified_match(self):
        manufacturer_id = self.connection.execute("SELECT id FROM manufacturers WHERE name='Test Vendor'").fetchone()[0]
        source_file_id = self.connection.execute('SELECT id FROM source_files').fetchone()[0]
        add_model_only_alias(self.connection, manufacturer_id, source_file_id)
        self.assertEqual(0, self.search(query='UPS-1')['total'])
        result = self.search(query='UPS-1', mode='nearby', includeUnknown=True)
        self.assertEqual(1, result['total'])
        self.assertEqual('unknown', result['models'][0]['status'])
        self.assertFalse(result['models'][0]['identityVerified'])
        self.assertFalse(result['models'][0]['canOptimize'])

    def test_catalog_does_not_treat_legacy_runtime_as_verified_configuration(self):
        self.curves[0].pop('batteryConfigurations')
        result = self.search(query='UPS-1', mode='nearby', includeUnknown=True,
                             requireOutlets=False)
        self.assertEqual(1, result['total'])
        model = result['models'][0]
        self.assertEqual('unknown', model['checks']['runtime']['status'])
        self.assertIsNone(model['runtimeMinutes'])

    def test_catalog_does_not_certify_unverified_legacy_capacity(self):
        self.curves[0].pop('ratingWattsVerified')
        self.curves[0].pop('ratingVaVerified')
        result = self.search(query='UPS-1', mode='nearby', includeUnknown=True,
                             requireOutlets=False, requireRuntime=False)
        self.assertEqual(1, result['total'])
        model = result['models'][0]
        self.assertEqual('unknown', model['checks']['capacityWatts']['status'])
        self.assertEqual('unknown', model['checks']['capacityVa']['status'])

    def test_catalog_exposes_each_verified_battery_configuration(self):
        curve = {'manufacturer': 'Test Vendor', 'model': 'UPS-1', 'sku': 'UPS-1',
                 'identityVerified': True, 'ratingWattsVerified': True, 'ratedWatts': 700,
                 'ratingVaVerified': True, 'ratedVa': 900,
                 'points': [{'loadW': 1, 'runtimeMinutes': 60}, {'loadW': 700, 'runtimeMinutes': 20}],
                 'outletGroups': [{'type': 'IEC_C13', 'power': 'battery', 'manufacturer_verified': True, 'count': 4}],
                 'batteryConfigurations': [
                     {'id': 'internal', 'manufacturer_verified': True, 'label': 'Internal', 'points': [
                         {'loadW': 1, 'runtimeMinutes': 8}, {'loadW': 700, 'runtimeMinutes': 2}],
                      'includedPacks': [], 'extraPacks': []},
                     {'id': 'plus-one', 'manufacturer_verified': True, 'label': 'One extra pack', 'points': [
                         {'loadW': 1, 'runtimeMinutes': 120}, {'loadW': 700, 'runtimeMinutes': 40}],
                      'includedPacks': [], 'extraPacks': [{'sku': 'EBM-1', 'quantity': 1}]},
                 ], 'source': {'url': 'https://vendor.example/ups-1.pdf'}}
        result = search_catalog_from_connection(self.connection, {
            'query': 'UPS-1', 'requiredWatts': 400, 'requiredVa': 500,
            'requiredOutlets': 2, 'runtimeRequirementMinutes': 10,
        }, curve_catalog=[curve])
        self.assertEqual(1, result['total'])
        model = result['models'][0]
        self.assertTrue(model['canOptimize'])
        self.assertEqual({'internal', 'plus-one'}, {item['id'] for item in model['batteryConfigurations']})
        self.assertEqual('pass', model['checks']['runtime']['status'])

    def test_runtime_toggle_only_disables_runtime_requirement(self):
        result = self.search(requiredWatts=50, requiredVa=62.5, requireRuntime=False)
        self.assertGreater(result['total'], 0)
        self.assertTrue(all(not m['checks']['runtime']['enabled'] for m in result['models']))
        self.assertTrue(all(m['runtimeMinutes'] is None for m in result['models']))

    def test_nearby_has_measurable_shortfall_and_tolerance(self):
        strict = self.search(query='UPS-TOO-SMALL', requireRuntime=False)
        self.assertEqual(0, strict['total'])
        nearby = self.search(query='UPS-TOO-SMALL', requireRuntime=False, mode='nearby')
        self.assertEqual(1, nearby['total'])
        model = nearby['models'][0]
        self.assertEqual('nearby', model['status'])
        self.assertEqual(40, model['checks']['capacityWatts']['shortfall'])
        self.assertEqual(0, self.search(query='UPS-TOO-SMALL', requireRuntime=False,
                         mode='nearby', nearbyTolerancePercent=5)['total'])

    def test_search_facets_pagination_expansion_and_privacy(self):
        result = self.search(requiredWatts=0, requiredVa=0, runtimeRequirementMinutes=0,
                             expansion='supported', pageSize=2)
        self.assertEqual(2, len(result['models']))
        self.assertTrue(all(m['expansion']['supported'] for m in result['models']))
        self.assertIn('Test Vendor', result['facets']['manufacturers'])
        encoded = json.dumps(result).casefold()
        for secret in ('synthetic/', 'source_locator', 'dealer', 'estimated_cost', 'price_offers'):
            self.assertNotIn(secret, encoded)

    def test_rejects_invalid_controls(self):
        for update in ({'headroomPercent':51}, {'mode':'anything'}, {'includeUnknown':1},
                       {'requiredWatts':float('nan')}, {'pageSize':26}, {'page':0}):
            with self.subTest(update=update), self.assertRaises(RecommendationInputError):
                self.search(**update)

    def test_budget_uses_normalized_prices_and_excludes_unknown_vat(self):
        result = self.search(maxPriceTHB=9500)
        self.assertEqual(['UPS-1'], [m['model'] for m in result['models']])
        self.assertEqual({'amount': 9000.0, 'currency': 'THB', 'vatBasis': 'exclusive'},
                         result['models'][0]['price'])
        self.connection.execute("UPDATE price_offers SET vat_treatment='unknown'")
        self.assertEqual(0, self.search(maxPriceTHB=9500)['total'])
        unfiltered = self.search()
        self.assertTrue(all(m['price'] is None for m in unfiltered['models']))
        self.assertTrue(all(m['priceUnavailableReason'] == 'missing_vat' for m in unfiltered['models']))

    def test_strict_does_not_admit_unknown_when_include_unknown_true(self):
        self.assertEqual(0, self.search(requiredWatts=50, requiredVa=62.5, includeUnknown=True)['total'])

    def test_runtime_unknown_reasons_distinguish_above_missing_and_no_load(self):
        above = self.search(requiredWatts=1300, requiredVa=1400, headroomPercent=0,
                            mode='nearby', includeUnknown=True, nearbyTolerancePercent=50,
                            query='UPS-6')
        self.assertEqual('load_above_curve_range', above['models'][0]['checks']['runtime']['reason'])
        no_load = self.search(requiredWatts=0, requiredVa=0, mode='nearby', includeUnknown=True)
        self.assertTrue(all(m['checks']['runtime']['reason'] == 'load_not_specified' for m in no_load['models']))
        self.connection.execute('UPDATE product_sources SET raw_text=?', ('missing runtime',))
        for curve in self.curves:
            curve.pop('batteryConfigurations', None)
        missing = self.search(mode='nearby', includeUnknown=True)
        self.assertTrue(all(m['checks']['runtime']['reason'] == 'missing_runtime_curve' for m in missing['models']))

    def test_dataset_metadata_is_optional_whitelisted_and_typed(self):
        self.assertIsNone(self.search()['dataset'])
        self.connection.execute('CREATE TABLE catalog_meta(key TEXT,value TEXT)')
        self.connection.executemany('INSERT INTO catalog_meta VALUES(?,?)', [
            ('format', 'ups-curated-v1'), ('model_count', '223'),
            ('original_model_count', '782'), ('omitted_missing_capacity_count', '559'),
            ('includes_prices', 'true'), ('includes_incomplete', 'false'),
            ('private_source_path', 'private-do-not-return'),
        ])
        dataset = self.search()['dataset']
        self.assertEqual(223, dataset['model_count'])
        self.assertEqual(782, dataset['original_model_count'])
        self.assertEqual(559, dataset['omitted_missing_capacity_count'])
        self.assertIs(True, dataset['includes_prices'])
        self.assertIs(False, dataset['includes_incomplete'])
        self.assertNotIn('private_source_path', dataset)


if __name__ == '__main__':
    unittest.main()
