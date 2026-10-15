"""Loopback-only cloud handler tests using synthetic catalogs and credentials."""
import base64
import http.client
import json
from pathlib import Path
import sqlite3
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cloud_api import create_cloud_server, hash_password, parse_users, ROUTES
from test_recommendation_engine import seed_catalog


class SyntheticStore:
    calls = 0
    def connect(self):
        self.calls += 1
        connection = sqlite3.connect(':memory:', isolation_level=None)
        seed_catalog(connection)
        return connection


class CloudApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.users = json.dumps({'synthetic': hash_password('synthetic-test-password', 100000)})
        cls.auth = 'Basic ' + base64.b64encode(b'synthetic:synthetic-test-password').decode('ascii')

    def setUp(self):
        self.store = SyntheticStore()
        self.now = 0.0
        self.server = create_cloud_server(self.store, self.users, 'localhost', port=0,
                                          clock=lambda: self.now)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, method='GET', path='/', body=None, **overrides):
        headers = {'Host': 'localhost', 'X-Forwarded-Proto': 'https', 'Authorization': self.auth}
        for key, value in overrides.items():
            if value is None:
                headers.pop(key, None)
            else:
                headers[key] = value
        if body is not None:
            headers.setdefault('Content-Type', 'application/json')
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_entire_page_and_api_require_authentication(self):
        for path in ('/', '/optimization.js', '/styles/tailwind.css'):
            status, headers, body = self.request(path=path, Authorization=None)
            self.assertEqual(401, status)
            self.assertIn('Basic realm=', headers['WWW-Authenticate'])
            self.assertEqual('no-store', headers['Cache-Control'])
        status, _, _ = self.request('POST', '/api/catalog/search', '{}', Authorization=None)
        self.assertEqual(401, status)
        self.assertEqual(0, self.store.calls)

    def test_wrong_password_and_unknown_user_are_rejected(self):
        for credentials in (b'synthetic:wrong', b'unknown:synthetic-test-password'):
            auth = 'Basic ' + base64.b64encode(credentials).decode('ascii')
            self.assertEqual(401, self.request(Authorization=auth)[0])

    def test_health_is_minimal_and_does_not_touch_database(self):
        status, _, body = self.request(path='/health', Authorization=None)
        self.assertEqual(200, status)
        self.assertEqual({'status': 'ok'}, json.loads(body))
        self.assertEqual(0, self.store.calls)

    def test_host_origin_and_https_guards(self):
        self.assertEqual(403, self.request(Host='attacker.example')[0])
        self.assertEqual(403, self.request(Origin='https://attacker.example')[0])
        self.assertEqual(403, self.request(**{'X-Forwarded-Proto': 'http'})[0])
        self.assertEqual(200, self.request(Origin='https://localhost')[0])

    def test_auth_before_catalog_and_no_private_source_paths(self):
        status, _, body = self.request('POST', '/api/catalog/search', '{}')
        self.assertEqual(200, status)
        self.assertIn('models', json.loads(body))
        self.assertNotIn(b'SYNTHETIC/', body)
        self.assertEqual(1, self.store.calls)

    def test_rejects_malformed_content_nonfinite_and_oversized_bodies(self):
        for body in ('{', '[]', '{"x":NaN}', '{"x":1e999}'):
            self.assertEqual(400, self.request('POST', '/api/catalog/search', body)[0])
        self.assertEqual(415, self.request('POST', '/api/catalog/search', '{}', **{'Content-Type':'text/plain'})[0])
        self.assertEqual(413, self.request('POST', '/api/catalog/search', '{}', **{'Content-Length':'1000001'})[0])
        self.assertEqual(404, self.request('POST', '/api/sql', '{}')[0])

    def test_backend_failure_is_generic(self):
        with patch.object(self.store, 'connect', side_effect=RuntimeError('private-token-path-secret')):
            status, _, body = self.request('POST', '/api/catalog/search', '{}')
        self.assertEqual(503, status)
        self.assertEqual({'error':'Service unavailable'}, json.loads(body))

    def test_busy_optimizer_is_bounded(self):
        entered, release = threading.Event(), threading.Event()
        responses = []
        def slow(connection, payload):
            entered.set()
            release.wait(3)
            return {'ok':True}
        with patch.dict(ROUTES, {'/api/optimizations': slow}):
            first = threading.Thread(target=lambda: responses.append(self.request('POST', '/api/optimizations', '{}')[0]))
            first.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(429, self.request('POST', '/api/optimizations', '{}')[0])
            finally:
                release.set()
                first.join(timeout=4)
        self.assertEqual([200], responses)

    def test_configuration_requires_real_hashed_users(self):
        for users in ('{}', '{"team":"plaintext"}', 'not-json'):
            with self.assertRaises(ValueError):
                parse_users(users)

    def test_five_failed_logins_throttle_and_expire_without_waiting(self):
        wrong = 'Basic ' + base64.b64encode(b'synthetic:wrong').decode('ascii')
        for _ in range(5):
            self.assertEqual(401, self.request(Authorization=wrong)[0])
        status, _, body = self.request()
        self.assertEqual(429, status)
        self.assertNotIn(b'synthetic', body)
        self.now = 61.0
        self.assertEqual(200, self.request()[0])

    def test_successful_login_clears_prior_failures(self):
        wrong = 'Basic ' + base64.b64encode(b'synthetic:wrong').decode('ascii')
        for _ in range(4):
            self.assertEqual(401, self.request(Authorization=wrong)[0])
        self.assertEqual(200, self.request()[0])
        for _ in range(4):
            self.assertEqual(401, self.request(Authorization=wrong)[0])
        self.assertEqual(200, self.request()[0])

    def test_authentication_hashing_has_two_concurrent_slots(self):
        import cloud_api
        original = cloud_api.hashlib.pbkdf2_hmac
        entered, release = threading.Event(), threading.Event()
        lock = threading.Lock()
        calls = []
        responses = []
        def slow_hash(*args, **kwargs):
            with lock:
                calls.append(1)
                if len(calls) == 2:
                    entered.set()
            release.wait(3)
            return original(*args, **kwargs)
        with patch('cloud_api.hashlib.pbkdf2_hmac', side_effect=slow_hash):
            threads = [threading.Thread(target=lambda: responses.append(self.request()[0])) for _ in range(2)]
            for thread in threads:
                thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(429, self.request()[0])
                self.assertEqual(2, len(calls))
            finally:
                release.set()
                for thread in threads:
                    thread.join(timeout=4)
        self.assertEqual([200, 200], responses)

    def test_explicit_loopback_development_flag(self):
        dev = create_cloud_server(self.store, self.users, 'localhost', port=0, allow_insecure=True)
        thread = threading.Thread(target=dev.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection('127.0.0.1', dev.server_port)
        try:
            connection.request('GET', '/', headers={'Host':'localhost','Authorization':self.auth})
            response = connection.getresponse()
            self.assertEqual(200, response.status)
            response.read()
        finally:
            connection.close()
            dev.shutdown()
            dev.server_close()
            thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
