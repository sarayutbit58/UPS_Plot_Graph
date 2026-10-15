"""HTTP acceptance cases for the local recommendation API."""

import json
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
from test_recommendation_engine import seed_catalog


class RecommendationApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / 'catalog.sqlite3'
        disk = sqlite3.connect(self.db_path, isolation_level=None)
        self.catalog = seed_catalog(disk)
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
        self.assertNotIn('synthetic/', json.dumps(payload))
        with urlopen(self.url + '/catalog.js', timeout=3) as response:
            self.assertEqual(200, response.status)

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
            connection.execute("UPDATE price_offers SET currency='THB',vat_treatment='exclusive'")
            source_id = connection.execute('''SELECT id FROM product_sources
                WHERE product_id=? AND source_locator=?''',
                (product_id, f'sheet:UPS;row:{product_id}')).fetchone()[0]
            raw_json = json.loads(connection.execute(
                'SELECT raw_json FROM product_sources WHERE id=?', (source_id,)
            ).fetchone()[0])
            raw_json.update(price_effective_from='2026-05-01', price_effective_to='2026-05-31')
            connection.execute('UPDATE product_sources SET raw_json=? WHERE id=?',
                               (json.dumps(raw_json), source_id))
            connection.execute('''UPDATE price_offers
                SET amount_decimal='9630',vat_treatment='inclusive',source_date='2026-05-10'
                WHERE product_source_id=?''', (source_id,))
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
