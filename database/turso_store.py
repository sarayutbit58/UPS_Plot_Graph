"""Small bounded Turso HTTP client; credentials remain on the Python server."""

import json
import sqlite3
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

try:
    from .published_catalog import SCHEMA, FORMAT
except ImportError:
    from published_catalog import SCHEMA, FORMAT

MAX_RESPONSE_BYTES = 8_000_000
MAX_MODELS = 10_000


class TursoError(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TursoError('Database redirects are not allowed')


def _value(value):
    if value is None:
        return {'type': 'null'}
    if isinstance(value, int):
        return {'type': 'integer', 'value': str(value)}
    if isinstance(value, float):
        return {'type': 'float', 'value': value}
    return {'type': 'text', 'value': str(value)}


def decode_rows(result):
    rows = []
    for row in result.get('rows', []):
        decoded = []
        for cell in row:
            kind = cell.get('type')
            if kind == 'null':
                decoded.append(None)
            elif kind == 'integer':
                decoded.append(int(cell['value']))
            elif kind == 'float':
                decoded.append(float(cell['value']))
            elif kind == 'text':
                decoded.append(cell['value'])
            else:
                raise TursoError('Unsupported database value')
        rows.append(tuple(decoded))
    return rows


class TursoStore:
    def __init__(self, url, token, cache_seconds=60):
        if url.startswith('libsql://'):
            url = 'https://' + url[len('libsql://'):]
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ('', '/')):
            raise ValueError('Turso URL must be an HTTPS database origin')
        if not token or '\n' in token or '\r' in token:
            raise ValueError('A server-side database token is required')
        self._endpoint = url.rstrip('/') + '/v2/pipeline'
        self._token = token
        self._cache_seconds = max(0, float(cache_seconds))
        self._lock = threading.Lock()
        self._cached = None
        self._expires = 0

    def pipeline(self, requests):
        body = json.dumps({'requests': requests + [{'type': 'close'}]},
                          allow_nan=False).encode()
        request = Request(self._endpoint, data=body, method='POST', headers={
            'Authorization': 'Bearer ' + self._token, 'Content-Type': 'application/json'})
        try:
            with build_opener(_NoRedirect()).open(request, timeout=15) as response:
                data = response.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise TursoError('Database response exceeds size limit')
            result = json.loads(data)
        except (HTTPError, URLError, TimeoutError, ValueError, OSError) as error:
            raise TursoError('Database HTTP request failed') from None
        results = result.get('results', [])
        if len(results) != len(requests) + 1 or any(item.get('type') != 'ok' for item in results):
            raise TursoError('Database statement failed')
        return [item['response'] for item in results[:-1]]

    def query(self, sql, args=()):
        return decode_rows(self.pipeline([{'type': 'execute', 'stmt': {
            'sql': sql, 'args': [_value(value) for value in args], 'want_rows': True}}])[0]['result'])

    def connect(self):
        with self._lock:
            if self._cached is None or time.monotonic() >= self._expires:
                # One batch read transaction gives a consistent snapshot during publication.
                responses = self.pipeline([{'type': 'batch', 'batch': {'steps': [
                    {'stmt': {'sql': 'BEGIN', 'want_rows': False}},
                    {'stmt': {'sql': 'SELECT key,value FROM catalog_meta', 'want_rows': True}},
                    {'stmt': {'sql': 'SELECT product_id,manufacturer,model,sku,rating_w,rating_va,outlet_count,model_json FROM published_models ORDER BY product_id LIMIT 10001', 'want_rows': True}},
                    {'stmt': {'sql': 'COMMIT', 'want_rows': False}},
                ]}}])
                batch = responses[0]['result']
                if any(batch.get('step_errors', [])):
                    raise TursoError('Database catalog read failed')
                steps = batch['step_results']
                metadata, models = decode_rows(steps[1]), decode_rows(steps[2])
                if dict(metadata).get('format') != FORMAT or len(models) > MAX_MODELS:
                    raise TursoError('Unsupported catalog format or size')
                self._cached = (metadata, models)
                self._expires = time.monotonic() + self._cache_seconds
            metadata, models = self._cached
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA)
        connection.executemany('INSERT INTO catalog_meta VALUES(?,?)', metadata)
        connection.executemany('INSERT INTO published_models VALUES(?,?,?,?,?,?,?,?)', models)
        connection.commit()
        connection.execute('PRAGMA query_only=ON')
        return connection
