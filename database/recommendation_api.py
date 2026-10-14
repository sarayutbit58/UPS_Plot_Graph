"""Local-only HTTP API for the UPS recommendation engine."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3

try:
    from .allocation_engine import AllocationInputError, allocate
    from .optimization_engine import optimize
    from .recommendation_engine import RecommendationInputError, recommend
except ImportError:  # Running this file directly keeps the database folder on sys.path.
    from allocation_engine import AllocationInputError, allocate
    from optimization_engine import optimize
    from recommendation_engine import RecommendationInputError, recommend


MAX_REQUEST_BYTES = 1_000_000
DEFAULT_DATABASE = Path(__file__).resolve().parent / 'local' / 'ups-products.sqlite3'
PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_FILES = {
    '/': ('index.html', 'text/html; charset=utf-8'),
    '/index.html': ('index.html', 'text/html; charset=utf-8'),
    '/calculations.js': ('calculations.js', 'text/javascript; charset=utf-8'),
    '/optimization.js': ('optimization.js', 'text/javascript; charset=utf-8'),
    '/styles/tailwind.css': ('styles/tailwind.css', 'text/css; charset=utf-8'),
}


def create_server(database_path, port=8000):
    """Create a server bound only to loopback so local pricing is not exposed to a LAN."""
    path = Path(database_path).resolve()

    class Handler(BaseHTTPRequestHandler):
        server_version = 'UPSRecommendationAPI/1.0'

        def _send_json(self, status, body):
            encoded = json.dumps(body, ensure_ascii=False, allow_nan=False,
                                 separators=(',', ':')).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(encoded)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            static_entry = STATIC_FILES.get(self.path)
            if static_entry:
                file_path = PROJECT_ROOT / static_entry[0]
                try:
                    body = file_path.read_bytes()
                except OSError:
                    self._send_json(404, {'error': 'Not found'})
                    return
                self.send_response(200)
                self.send_header('Content-Type', static_entry[1])
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-cache')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path != '/health':
                self._send_json(404, {'error': 'Not found'})
                return
            try:
                with sqlite3.connect(str(path), timeout=5) as connection:
                    count = connection.execute("SELECT count(*) FROM products WHERE product_type='UPS'").fetchone()[0]
                self._send_json(200, {'status': 'ok', 'upsProducts': count})
            except sqlite3.Error:
                self._send_json(503, {'error': 'Recommendation database is unavailable'})

        def do_POST(self):
            if self.path not in {'/api/recommendations', '/api/allocations', '/api/optimizations'}:
                self._send_json(404, {'error': 'Not found'})
                return
            content_type = self.headers.get('Content-Type', '').split(';', 1)[0].strip().casefold()
            if content_type != 'application/json':
                self._send_json(415, {'error': 'Content-Type must be application/json'})
                return
            try:
                content_length = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                self._send_json(400, {'error': 'Invalid Content-Length'})
                return
            if content_length < 1 or content_length > MAX_REQUEST_BYTES:
                self._send_json(413, {'error': f'Request body must be 1 to {MAX_REQUEST_BYTES} bytes'})
                return
            try:
                payload = json.loads(self.rfile.read(content_length).decode('utf-8'))
                if self.path == '/api/allocations':
                    result = allocate(path, payload)
                elif self.path == '/api/optimizations':
                    result = optimize(path, payload)
                else:
                    result = recommend(path, payload)
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(400, {'error': 'Request body is not valid UTF-8 JSON'})
                return
            except (RecommendationInputError, AllocationInputError) as error:
                self._send_json(400, {'error': str(error)})
                return
            except ValueError:
                self._send_json(503, {'error': 'Recommendation data could not be processed'})
                return
            except FileNotFoundError:
                self._send_json(503, {'error': 'Recommendation database is unavailable'})
                return
            except sqlite3.Error:
                self._send_json(503, {'error': 'Recommendation database could not be read'})
                return
            self._send_json(200, result)

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=DEFAULT_DATABASE)
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args(argv)
    if not args.db.is_file():
        parser.error(f'Database not found: {args.db}')
    if not 0 <= args.port <= 65535:
        parser.error('--port must be between 0 and 65535')
    server = create_server(args.db, args.port)
    print(f'UPS recommendation API listening on http://127.0.0.1:{server.server_address[1]}')
    print('GET /  POST /api/recommendations  POST /api/allocations  POST /api/optimizations  GET /health  (loopback only)')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
