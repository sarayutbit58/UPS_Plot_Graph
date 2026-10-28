"""HTTP acceptance cases for the local recommendation API."""

import json
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from database.recommendation_api import create_server
from database.published_catalog import SCHEMA, project_model


def seed_verified_api_catalog(connection):
    """Synthetic v2 catalog with explicit OEM provenance and private offer paths."""
    connection.executescript(SCHEMA)
    products = []
    for index in range(7):
        product_id = index + 1
        sku = f'API-UPS-{product_id}'
        watts = 700 + 100 * index if index < 6 else 440
        va = 900 + 100 * index if index < 6 else 550
        outlets = 4 + index if index < 6 else 4
        source = {'url': f'https://www.se.com/synthetic/{sku}'}
        price = {
            'amount': str(9000 + 1000 * index), 'currency': 'THB',
            'vatTreatment': 'exclusive', 'sourceDate': '2026-05-10',
            'priceEffectiveFrom': None, 'priceEffectiveTo': None,
            'priceType': 'NDP', 'kind': 'NDP',
            'source': {'path': 'SYNTHETIC/private-pricelist', 'locator': f'row:{product_id}'},
        }
        product = {
            'id': product_id, 'manufacturer_id': 1, 'manufacturer': 'APC',
            'model': sku, 'sku': sku, 'product_type': 'UPS', 'non_ups_reason': None,
            'rating_w': watts, 'rating_va': va, 'outlet_count': outlets,
            'external_battery_supported': index % 2 == 0,
            'external_battery_model': 'API-BATTERY' if index % 2 == 0 else None,
            'max_external_battery_packs': 2 if index % 2 == 0 else None,
            'identity_verified': True, 'manufacturer_sku': sku,
            'identity_source': source, 'product_category': 'UPS',
            'rating_w_verified': True, 'rating_w_source': source,
            'rating_va_verified': True, 'rating_va_source': source,
            'outlet_groups': [{'type': 'IEC_C13', 'power': 'battery', 'count': outlets,
                               'manufacturer_verified': True, 'source': source}],
            'battery_configurations': [{
                'id': 'internal', 'label': 'Synthetic internal battery',
                'manufacturer_verified': True, 'source': source,
                'points': [{'loadW': 1, 'runtimeMinutes': 60},
                           {'loadW': watts, 'runtimeMinutes': 20}],
                'includedPacks': [], 'extraPacks': [], 'conditions': 'synthetic test data',
            }],
            'runtime_curve': None, 'warnings': [],
            'evidence': [{'path': 'SYNTHETIC/private-source', 'locator': f'row:{product_id}',
                          'ratingWEvidence': True, 'ratingVaEvidence': True}],
            'prices': [price], 'estimated_cost': price,
        }
        projected = project_model(product, include_prices=True)
        connection.execute('INSERT INTO published_models VALUES(?,?,?,?,?,?,?,?)',
                           (product_id, 'APC', sku, sku, watts, va, outlets,
                            json.dumps(projected, ensure_ascii=False)))
        products.append(product_id)
    connection.executemany('INSERT INTO catalog_meta VALUES(?,?)', [
        ('format', 'ups-curated-v2'), ('model_count', str(len(products))),
        ('generated_at', '2026-10-28T00:00:00Z'), ('includes_prices', 'true'),
        ('includes_incomplete', 'false'),
    ])
    return {'products': products}


class RecommendationApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / 'catalog.sqlite3'
        disk = sqlite3.connect(self.db_path, isolation_level=None)
        self.catalog = seed_verified_api_catalog(disk)
        disk.close()
        self.server = create_server(self.db_path, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def post(self, path, payload):
        request = Request(self.url + path, data=json.dumps(payload).encode('utf-8'),
                          headers={'Content-Type': 'application/json'}, method='POST')
        with urlopen(request, timeout=5) as response:
            self.assertEqual(200, response.status)
            return json.loads(response.read())

    def update_model(self, product_id, transform, catalog_format=None):
        with closing(sqlite3.connect(self.db_path)) as connection:
            model = json.loads(connection.execute(
                'SELECT model_json FROM published_models WHERE product_id=?', (product_id,)
            ).fetchone()[0])
            transform(model)
            connection.execute('UPDATE published_models SET model_json=? WHERE product_id=?',
                               (json.dumps(model), product_id))
            if catalog_format:
                connection.execute("UPDATE catalog_meta SET value=? WHERE key='format'", (catalog_format,))
            connection.commit()

    def test_post_returns_top_five_json_recommendations(self):
        body = json.dumps({
            'devices': [{'name': 'Server', 'quantity': 1, 'watt': 400, 'va': 500}],
            'powerFactor': 0.8,
            'runtimeRequirementMinutes': 10,
        }).encode('utf-8')
        request = Request(self.url + '/api/recommendations', data=body,
                          headers={'Content-Type': 'application/json'}, method='POST')
        with urlopen(request, timeout=3) as response:
            self.assertEqual(200, response.status)
            result = json.loads(response.read())
        self.assertEqual(5, len(result['recommendations']))
        self.assertIn('rankingFormula', result)
        encoded = json.dumps(result).casefold()
        self.assertNotIn('synthetic/private-source', encoded)
        self.assertNotIn('synthetic/private-pricelist', encoded)

    def test_catalog_search_returns_bounded_rows_without_raw_source_records(self):
        body = json.dumps({'mode': 'nearby', 'includeUnknown': True,
            'requiredWatts': 50, 'requiredVa': 62.5, 'requiredOutlets': 2,
            'runtimeRequirementMinutes': 10, 'pageSize': 2}).encode()
        request = Request(self.url + '/api/catalog/search', data=body,
            headers={'Content-Type': 'application/json'}, method='POST')
        with urlopen(request, timeout=3) as response:
            payload = json.loads(response.read())
        self.assertEqual(2, len(payload['models']))
        self.assertGreater(payload['total'], 2)
        self.assertNotIn('raw_json', json.dumps(payload))
        for forbidden in ('synthetic/private-source', 'synthetic/private-pricelist'):
            self.assertNotIn(forbidden, json.dumps(payload).casefold())
        with urlopen(self.url + '/catalog.js', timeout=3) as response:
            self.assertEqual(200, response.status)

    def test_v2_catalog_uses_verified_capacity_outlets_and_runtime_over_http(self):
        result = self.post('/api/catalog/search', {
            'query': 'API-UPS-1', 'requiredWatts': 400, 'requiredVa': 500,
            'requiredOutlets': 2, 'runtimeRequirementMinutes': 10,
        })
        self.assertEqual('ups-curated-v2', result['dataset']['format'])
        self.assertEqual(1, result['total'])
        model = result['models'][0]
        self.assertTrue(model['identityVerified'])
        self.assertTrue(model['canOptimize'])
        self.assertEqual(4, model['batteryBackupOutletCount'])
        self.assertTrue(all(check['status'] == 'pass' for check in model['checks'].values()
                            if check['enabled']))
        self.assertNotIn('SYNTHETIC/private', json.dumps(result))

    def test_v1_legacy_fields_fail_closed_across_all_four_endpoints(self):
        product_id = self.catalog['products'][0]
        def make_legacy(model):
            for key in ('identity_verified', 'manufacturer_sku', 'identity_source',
                        'rating_w_verified', 'rating_w_source', 'rating_va_verified',
                        'rating_va_source', 'outlet_groups', 'battery_configurations'):
                model.pop(key, None)
            model['runtime_curve'] = {
                'points': [{'loadW': 1, 'runtimeMinutes': 60},
                           {'loadW': 700, 'runtimeMinutes': 20}],
                'batteryConfiguration': 'unverified legacy',
                'source': {'url': 'https://www.se.com/synthetic/legacy'},
            }
        self.update_model(product_id, make_legacy, 'ups-curated-v1')
        query = {'query': 'API-UPS-1', 'requiredWatts': 400, 'requiredVa': 500,
                 'requiredOutlets': 2, 'runtimeRequirementMinutes': 10}
        self.assertEqual(0, self.post('/api/catalog/search', query)['total'])
        nearby = self.post('/api/catalog/search', {**query, 'mode': 'nearby', 'includeUnknown': True})
        self.assertEqual(1, nearby['total'])
        model = nearby['models'][0]
        self.assertEqual('unknown', model['status'])
        self.assertFalse(model['identityVerified'])
        self.assertIsNone(model['batteryBackupOutletCount'])
        self.assertIsNone(model['runtimeMinutes'])
        self.assertFalse(model['canOptimize'])
        request = {'runtimeRequirementMinutes': 10,
                   'devices': [{'name': 'Server', 'quantity': 1, 'watt': 400,
                                'va': 500, 'priority': 50}]}
        recommendations = self.post('/api/recommendations', {**request, 'powerFactor': 0.8})
        self.assertNotIn(product_id, [item['productId'] for item in recommendations['recommendations']])
        allocation = self.post('/api/allocations', {**request, 'selectedUps': {
            'mode': 'fixed', 'models': [{'productId': product_id, 'quantity': 1}]}})
        self.assertFalse(allocation['validation']['isValid'])
        self.assertEqual([], allocation['deviceAllocations'])
        self.assertIn('identity_unverified', allocation['unassignedDevices'][0]['reasons'])
        optimization = self.post('/api/optimizations', {**request, 'selectedProductIds': [product_id]})
        self.assertIsNone(optimization['bestSolution'])
        self.assertEqual(0, optimization['search']['candidateModelCount'])
        self.assertIn('identity_unverified', optimization['selectionDiagnostics']['excludedModels'][0]['reasons'])

    def test_untrusted_v2_battery_source_is_never_treated_as_verified(self):
        product_id = self.catalog['products'][0]
        def poison_source(model):
            model['outlet_groups'][0]['source']['url'] = 'https://se.com.attacker.example/outlet'
        self.update_model(product_id, poison_source)
        query = {'query': 'API-UPS-1', 'requiredWatts': 400, 'requiredVa': 500,
                 'requiredOutlets': 2, 'runtimeRequirementMinutes': 10,
                 'mode': 'nearby', 'includeUnknown': True}
        model = self.post('/api/catalog/search', query)['models'][0]
        self.assertIsNone(model['batteryBackupOutletCount'])
        self.assertFalse(model['canOptimize'])
        self.assertEqual('unknown', model['checks']['outlets']['status'])
        request = {'runtimeRequirementMinutes': 10,
                   'devices': [{'name': 'Server', 'quantity': 1, 'watt': 400,
                                'va': 500, 'priority': 50}],
                   'selectedProductIds': [product_id]}
        result = self.post('/api/optimizations', request)
        self.assertIsNone(result['bestSolution'])
        self.assertIn('outlet_unknown', result['selectionDiagnostics']['excludedModels'][0]['reasons'])

    def test_invalid_input_returns_a_json_400_error(self):
        body = b'{"devices":[],"powerFactor":0.8,"runtimeRequirementMinutes":10}'
        request = Request(self.url + '/api/recommendations', data=body,
                          headers={'Content-Type': 'application/json'}, method='POST')
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=3)
        self.assertEqual(400, error.exception.code)
        payload = json.loads(error.exception.read())
        self.assertIn('error', payload)

    def test_numeric_overflow_returns_a_json_400_error(self):
        body = json.dumps({
            'devices': [
                {'name': 'Load A', 'quantity': 1, 'watt': '1e308', 'va': '1e308'},
                {'name': 'Load B', 'quantity': 1, 'watt': '1e308', 'va': '1e308'},
            ],
            'powerFactor': 0.8,
            'runtimeRequirementMinutes': 10,
        }).encode('utf-8')
        request = Request(self.url + '/api/recommendations', data=body,
                          headers={'Content-Type': 'application/json'}, method='POST')
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=3)
        self.assertEqual(400, error.exception.code)
        payload = json.loads(error.exception.read())
        self.assertIn('supported numeric range', payload['error'])

    def test_post_allocates_devices_through_the_local_api(self):
        body = json.dumps({
            'selectedUps': {
                'mode': 'fixed',
                'models': [{'productId': self.catalog['products'][0], 'quantity': 1}],
            },
            'runtimeRequirementMinutes': 10,
            'devices': [{'name': 'Server', 'quantity': 1, 'watt': 400, 'va': 500, 'priority': 3}],
        }).encode('utf-8')
        request = Request(self.url + '/api/allocations', data=body,
                          headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urlopen(request, timeout=3) as response:
                self.assertEqual(200, response.status)
                result = json.loads(response.read())
        except HTTPError as error:
            self.fail(f'allocation endpoint returned HTTP {error.code}: {error.read()}')
        self.assertTrue(result['validation']['isValid'])
        self.assertEqual(1, len(result['deviceAllocations']))
        self.assertEqual(1, len(result['outletAllocations']))

    def test_http_allocation_mixes_models_and_checks_the_actual_connector(self):
        first, second = self.catalog['products'][:2]
        result = self.post('/api/allocations', {
            'selectedUps': {'mode': 'fixed', 'models': [
                {'productId': first, 'quantity': 1}, {'productId': second, 'quantity': 1}]},
            'runtimeRequirementMinutes': 10,
            'devices': [{'name': 'Server', 'quantity': 2, 'watt': 400, 'va': 500,
                         'priority': 50, 'plugs': [{'connector': 'IEC_C14', 'count': 1}]}],
        })
        self.assertTrue(result['validation']['isValid'])
        self.assertEqual({first, second}, {item['productId'] for item in result['deviceAllocations']})
        self.assertEqual({'IEC_C13'}, {item['outletType'] for item in result['outletAllocations']})
        self.assertEqual({'verified'}, {item['compatibilityStatus'] for item in result['outletAllocations']})
        self.assertNotIn('synthetic/private', json.dumps(result).casefold())

    def test_http_requires_selection_when_multiple_battery_configurations_exist(self):
        product_id = self.catalog['products'][0]
        def add_configuration(model):
            original = model['battery_configurations'][0]
            model['battery_configurations'].append({
                **original, 'id': 'plus-one', 'label': 'Synthetic external pack',
                'extraPacks': [{'sku': 'API-BATTERY', 'quantity': 1}],
                'points': [{'loadW': 1, 'runtimeMinutes': 120},
                           {'loadW': 700, 'runtimeMinutes': 40}],
            })
        self.update_model(product_id, add_configuration)
        request = {'runtimeRequirementMinutes': 10,
                   'devices': [{'name': 'Server', 'quantity': 1, 'watt': 400,
                                'va': 500, 'priority': 50}]}
        selected = {'mode': 'fixed', 'models': [{'productId': product_id, 'quantity': 1}]}
        unspecified = self.post('/api/allocations', {**request, 'selectedUps': selected})
        self.assertFalse(unspecified['validation']['isValid'])
        self.assertIn('runtime_unknown', unspecified['unassignedDevices'][0]['reasons'])
        selected['models'][0]['batteryConfigId'] = 'plus-one'
        result = self.post('/api/allocations', {**request, 'selectedUps': selected})
        self.assertTrue(result['validation']['isValid'])
        self.assertEqual('plus-one', result['upsUtilization'][0]['batteryConfigId'])
        optimized = self.post('/api/optimizations', {**request, 'selectedConfigurations': [
            {'productId': product_id, 'batteryConfigId': 'plus-one'}]})
        self.assertIsNotNone(optimized['bestSolution'])
        self.assertEqual('plus-one', optimized['bestSolution']['models'][0]['batteryConfigId'])
        self.assertFalse(optimized['priceComparison']['available'])
        self.assertEqual('battery_pack_price_unknown', optimized['priceComparison']['reasonCode'])
        self.assertNotIn('synthetic/private', json.dumps(optimized).casefold())

    def test_post_returns_optimization_scenarios_through_the_local_api(self):
        body = json.dumps({
            'runtimeRequirementMinutes': 5,
            'devices': [{'name': 'Server', 'quantity': 1, 'watt': 300, 'va': 375, 'priority': 50}],
        }).encode('utf-8')
        request = Request(self.url + '/api/optimizations', data=body,
                          headers={'Content-Type': 'application/json'}, method='POST')
        with urlopen(request, timeout=3) as response:
            self.assertEqual(200, response.status)
            result = json.loads(response.read())
        self.assertEqual('CSP', result['solver'])
        self.assertTrue(result['bestSolution']['validation']['isValid'])
        self.assertEqual({'A', 'B', 'C', 'D', 'E'}, {row['code'] for row in result['scenarios']})
        self.assertNotIn('synthetic/private', json.dumps(result).casefold())

    def test_api_rejects_untrusted_host_header(self):
        body = json.dumps({
            'runtimeRequirementMinutes': 5,
            'devices': [{'name': 'Server', 'quantity': 1, 'watt': 300, 'va': 375, 'priority': 50}],
        }).encode('utf-8')
        request = Request(self.url + '/api/optimizations', data=body,
                          headers={'Content-Type': 'application/json', 'Host': 'attacker.example'},
                          method='POST')

        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=3)
        self.assertEqual(403, error.exception.code)

    def test_api_rejects_untrusted_origin_header(self):
        body = json.dumps({
            'runtimeRequirementMinutes': 5,
            'devices': [{'name': 'Server', 'quantity': 1, 'watt': 300, 'va': 375, 'priority': 50}],
        }).encode('utf-8')
        request = Request(self.url + '/api/optimizations', data=body,
                          headers={'Content-Type': 'application/json', 'Origin': 'http://attacker.example'},
                          method='POST')

        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=3)
        self.assertEqual(403, error.exception.code)

    def test_api_accepts_loopback_origin_header(self):
        body = json.dumps({
            'runtimeRequirementMinutes': 5,
            'devices': [{'name': 'Server', 'quantity': 1, 'watt': 300, 'va': 375, 'priority': 50}],
        }).encode('utf-8')
        request = Request(self.url + '/api/optimizations', data=body,
                          headers={'Content-Type': 'application/json', 'Origin': self.url},
                          method='POST')

        with urlopen(request, timeout=3) as response:
            self.assertEqual(200, response.status)
            result = json.loads(response.read())
        self.assertEqual('CSP', result['solver'])

    def test_optimization_api_serializes_normalized_vat_metadata(self):
        product_id = self.catalog['products'][0]
        connection = sqlite3.connect(self.db_path)
        try:
            model = json.loads(connection.execute(
                'SELECT model_json FROM published_models WHERE product_id=?', (product_id,)
            ).fetchone()[0])
            for price in (model['prices'][0], model['estimated_cost']):
                price.update(amount='9630', vatTreatment='inclusive', sourceDate='2026-05-10',
                             priceEffectiveFrom='2026-05-01', priceEffectiveTo='2026-05-31')
            connection.execute('UPDATE published_models SET model_json=? WHERE product_id=?',
                               (json.dumps(model), product_id))
            connection.commit()
        finally:
            connection.close()
        body = json.dumps({
            'runtimeRequirementMinutes': 5,
            'devices': [{'name': 'Server', 'quantity': 1, 'watt': 400, 'va': 500, 'priority': 50}],
        }).encode('utf-8')
        request = Request(self.url + '/api/optimizations', data=body,
                          headers={'Content-Type': 'application/json'}, method='POST')

        with urlopen(request, timeout=3) as response:
            self.assertEqual(200, response.status)
            result = json.loads(response.read())

        self.assertEqual(7, result['priceComparison']['vatRatePercent'])
        self.assertEqual([{'startsOn': '2026-05-01', 'endsOn': '2026-05-31'}],
                         result['priceComparison']['priceEffectivePeriods'])
        cheapest = next(row for row in result['scenarios'] if row['code'] == 'A')
        self.assertTrue(cheapest['available'], result['priceComparison'])
        self.assertEqual(9000, cheapest['solution']['totalCost'])

    def test_get_serves_only_the_whitelisted_local_ui_files(self):
        with urlopen(self.url + '/', timeout=3) as response:
            self.assertEqual(200, response.status)
            self.assertIn(b'UPS Picker', response.read())
        with urlopen(self.url + '/optimization.js', timeout=3) as response:
            self.assertEqual(200, response.status)
            self.assertIn(b'UPSOptimization', response.read())
        with self.assertRaises(HTTPError) as error:
            urlopen(self.url + '/database/schema.sql', timeout=3)
        self.assertEqual(404, error.exception.code)

    def test_allocation_api_returns_partial_validation_when_fixed_capacity_is_short(self):
        body = json.dumps({
            'selectedUps': {
                'mode': 'fixed',
                'models': [{'productId': self.catalog['products'][0], 'quantity': 1}],
            },
            'runtimeRequirementMinutes': 10,
            'devices': [
                {'name': 'Server A', 'quantity': 1, 'watt': 400, 'va': 500, 'priority': 5},
                {'name': 'Server B', 'quantity': 1, 'watt': 400, 'va': 500, 'priority': 3},
            ],
        }).encode('utf-8')
        request = Request(self.url + '/api/allocations', data=body,
                          headers={'Content-Type': 'application/json'}, method='POST')
        with urlopen(request, timeout=3) as response:
            self.assertEqual(200, response.status)
            result = json.loads(response.read())
        self.assertFalse(result['validation']['isValid'])
        self.assertEqual(1, len(result['deviceAllocations']))
        self.assertEqual(1, result['unassignedDevices'][0]['quantity'])

    def test_allocation_api_rejects_missing_runtime_target(self):
        body = json.dumps({
            'selectedUps': {
                'mode': 'fixed',
                'models': [{'productId': self.catalog['products'][0], 'quantity': 1}],
            },
            'devices': [{'name': 'Server', 'quantity': 1, 'watt': 400, 'va': 500, 'priority': 3}],
        }).encode('utf-8')
        request = Request(self.url + '/api/allocations', data=body,
                          headers={'Content-Type': 'application/json'}, method='POST')
        with self.assertRaises(HTTPError) as error:
            urlopen(request, timeout=3)
        self.assertEqual(400, error.exception.code)
        payload = json.loads(error.exception.read())
        self.assertIn('runtimeRequirementMinutes', payload['error'])

    def test_allocation_api_returns_json_503_when_runtime_catalog_is_broken(self):
        body = json.dumps({
            'selectedUps': {
                'mode': 'fixed',
                'models': [{'productId': self.catalog['products'][0], 'quantity': 1}],
            },
            'runtimeRequirementMinutes': 10,
            'devices': [{'name': 'Server', 'quantity': 1, 'watt': 400, 'va': 500, 'priority': 3}],
        }).encode('utf-8')
        request = Request(self.url + '/api/allocations', data=body,
                          headers={'Content-Type': 'application/json'}, method='POST')
        with patch('database.allocation_engine._read_curve_catalog',
                   side_effect=ValueError('damaged runtime catalog')):
            try:
                urlopen(request, timeout=3)
                self.fail('Expected the invalid server-side runtime catalog to return HTTP 503')
            except HTTPError as error:
                self.assertEqual(503, error.code)
                payload = json.loads(error.read())
                self.assertEqual('Recommendation data could not be processed', payload['error'])
            except Exception as error:
                self.fail(f'Expected a controlled JSON error, got {type(error).__name__}: {error}')


if __name__ == '__main__':
    unittest.main()
