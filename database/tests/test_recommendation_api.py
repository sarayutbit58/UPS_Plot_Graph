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


if __name__ == '__main__':
    unittest.main()
