"""Synthetic HTTP fixtures; no cloud accounts or credentials required."""

import io
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from turso_store import TursoStore, TursoError, _NoRedirect, decode_rows


class TursoStoreTests(unittest.TestCase):
    def test_rejects_insecure_url_and_secret_in_url(self):
        for url in ('http://example.test', 'https://user:secret@example.test',
                    'https://example.test/?token=secret'):
            with self.assertRaises(ValueError):
                TursoStore(url, 'synthetic-token')
        self.assertTrue(TursoStore('libsql://example.test', 'synthetic-token')._endpoint.startswith('https://'))

    def test_redirect_never_forwards_credentials(self):
        with self.assertRaises(TursoError):
            _NoRedirect().redirect_request(Request('https://example.test'), None,
                                           302, '', {}, 'https://other.test')

    def test_query_decodes_tagged_cells_and_does_not_expose_token(self):
        response = {'results': [{'type': 'ok', 'response': {'type': 'execute',
            'result': {'rows': [[{'type': 'integer', 'value': '7'}, {'type': 'null'}]]}}},
            {'type': 'ok', 'response': {'type': 'close'}}]}
        with patch('turso_store.build_opener') as opener:
            opener.return_value.open.return_value = io.BytesIO(json.dumps(response).encode())
            store = TursoStore('https://example.test', 'synthetic-token')
            self.assertEqual(store.query('SELECT ?,NULL', (7,)), [(7, None)])
        self.assertNotIn('synthetic-token', repr(store))

    def test_failure_message_hides_remote_error_content(self):
        response = {'results': [{'type': 'error', 'error': {'message': 'private'}}]}
        with patch('turso_store.build_opener') as opener:
            opener.return_value.open.return_value = io.BytesIO(json.dumps(response).encode())
            with self.assertRaises(TursoError) as error:
                TursoStore('https://example.test', 'synthetic-token').query('SELECT 1')
        self.assertNotIn('private', str(error.exception))

    def test_cached_snapshot_gives_independent_readonly_connections(self):
        def tagged(value):
            return {'type': 'text', 'value': value}
        batch = {'step_results': [{}, {'rows': [[tagged('format'), tagged('ups-curated-v1')]]},
                                 {'rows': []}, {}], 'step_errors': [None] * 4}
        store = TursoStore('https://example.test', 'synthetic-token')
        with patch.object(store, 'pipeline', return_value=[{'result': batch}]) as request:
            first, second = store.connect(), store.connect()
        self.assertIsNot(first, second)
        self.assertEqual(request.call_count, 1)
        with self.assertRaises(sqlite3.OperationalError):
            first.execute('DELETE FROM catalog_meta')
        first.close()
        self.assertEqual(second.execute('SELECT count(*) FROM catalog_meta').fetchone()[0], 1)
        second.close()


if __name__ == '__main__':
    unittest.main()
