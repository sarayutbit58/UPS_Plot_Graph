"""Private team UPS service behind an HTTPS reverse proxy; standard library only."""
import argparse
import base64
import binascii
from collections import OrderedDict, deque
import getpass
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import secrets
import threading
import time
from urllib.parse import urlsplit

try:
    from .recommendation_api import STATIC_FILES, PROJECT_ROOT, MAX_REQUEST_BYTES
    from .recommendation_engine import recommend_from_connection, RecommendationInputError
    from .allocation_engine import allocate_from_connection, AllocationInputError
    from .optimization_engine import optimize_from_connection
    from .catalog_search import search_catalog_from_connection
    from .turso_store import TursoStore
except ImportError:
    from recommendation_api import STATIC_FILES, PROJECT_ROOT, MAX_REQUEST_BYTES
    from recommendation_engine import recommend_from_connection, RecommendationInputError
    from allocation_engine import allocate_from_connection, AllocationInputError
    from optimization_engine import optimize_from_connection
    from catalog_search import search_catalog_from_connection
    from turso_store import TursoStore

ROUTES = {'/api/recommendations': recommend_from_connection,
          '/api/allocations': allocate_from_connection,
          '/api/optimizations': optimize_from_connection,
          '/api/catalog/search': search_catalog_from_connection}


def hash_password(password, iterations=600000):
    if not isinstance(password, str) or not password:
        raise ValueError('A nonempty password is required')
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, iterations)
    return 'pbkdf2_sha256${}${}${}'.format(iterations,
        base64.b64encode(salt).decode('ascii'), base64.b64encode(digest).decode('ascii'))


def _parse_hash(encoded):
    try:
        algorithm, iterations, salt, digest = encoded.split('$')
        iterations = int(iterations)
        salt = base64.b64decode(salt, validate=True)
        digest = base64.b64decode(digest, validate=True)
        if algorithm != 'pbkdf2_sha256' or not 100000 <= iterations <= 2000000 or not 16 <= len(salt) <= 64 or len(digest) != 32:
            raise ValueError
        return iterations, salt, digest
    except (ValueError, TypeError, AttributeError, binascii.Error):
        raise ValueError('Invalid password hash configuration') from None


def parse_users(encoded):
    try:
        users = json.loads(encoded)
    except (ValueError, TypeError):
        raise ValueError('UPS_USERS_JSON must be a nonempty object of usernames and password hashes') from None
    if not isinstance(users, dict) or not users or len(users) > 100:
        raise ValueError('UPS_USERS_JSON must contain 1 to 100 users')
    parsed = {}
    for username, password_hash in users.items():
        if not isinstance(username, str) or not username or ':' in username or len(username) > 100:
            raise ValueError('Invalid username configuration')
        parsed[username] = _parse_hash(password_hash)
    return parsed


def _invalid_constant(value):
    raise ValueError('Nonfinite JSON values are not accepted')


def create_cloud_server(store, users_json, allowed_host, port=10000,
                        bind_host='127.0.0.1', allow_insecure=False, optimizer_concurrency=1,
                        clock=None):
    """Create a service; production main explicitly binds all interfaces."""
    users = parse_users(users_json)
    allowed_host = str(allowed_host or '').strip().lower()
    try:
        parsed_host = urlsplit('//' + allowed_host)
        parsed_host.port
        if (not parsed_host.hostname or parsed_host.username or parsed_host.password
                or parsed_host.path or parsed_host.query or parsed_host.fragment
                or any(ch.isspace() for ch in allowed_host)):
            raise ValueError
    except ValueError:
        raise ValueError('An exact allowed Host is required') from None
    if optimizer_concurrency not in (1, 2):
        raise ValueError('Optimizer concurrency must be 1 or 2')
    semaphore = threading.BoundedSemaphore(optimizer_concurrency)
    auth_slots = threading.BoundedSemaphore(2)
    auth_lock = threading.Lock()
    auth_failures = OrderedDict()
    auth_clock = clock or time.monotonic

    def throttled(address):
        now = auth_clock()
        with auth_lock:
            attempts = auth_failures.get(address)
            if attempts is None:
                return False
            while attempts and now - attempts[0] >= 60:
                attempts.popleft()
            if not attempts:
                del auth_failures[address]
                return False
            auth_failures.move_to_end(address)
            return len(attempts) >= 5

    def record_auth(address, authenticated):
        with auth_lock:
            if authenticated:
                auth_failures.pop(address, None)
            else:
                if address not in auth_failures:
                    if len(auth_failures) >= 1024:
                        auth_failures.popitem(last=False)
                    auth_failures[address] = deque(maxlen=5)
                auth_failures[address].append(auth_clock())
                auth_failures.move_to_end(address)

    class Handler(BaseHTTPRequestHandler):
        server_version = 'UPSPrivateAPI/1.0'

        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, format, *args):
            # HTTP request paths and credentials never enter service logs.
            pass

        def _send(self, status, body, content_type='application/json; charset=utf-8', challenge=False):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'same-origin')
            self.send_header('X-Frame-Options', 'DENY')
            if challenge:
                self.send_header('WWW-Authenticate', 'Basic realm="UPS Team", charset="UTF-8"')
            self.end_headers()
            self.wfile.write(body)

        def _guard(self):
            if len(self.headers.get_all('Host', [])) != 1 or self.headers.get('Host', '').lower() != allowed_host:
                self._send(403, {'error': 'Host is not allowed'})
                return False
            insecure = (allow_insecure and self.client_address[0] in {'127.0.0.1', '::1'}
                        and parsed_host.hostname in {'localhost', '127.0.0.1', '::1'})
            if not insecure and self.headers.get('X-Forwarded-Proto', '').lower() != 'https':
                self._send(403, {'error': 'HTTPS is required'})
                return False
            expected_origin = ('http://' if insecure else 'https://') + allowed_host
            origins = self.headers.get_all('Origin', [])
            if len(origins) > 1 or origins and origins[0] != expected_origin:
                self._send(403, {'error': 'Origin is not allowed'})
                return False
            authorization = self.headers.get('Authorization', '')
            if not authorization:
                self._send(401, {'error': 'Authentication required'}, challenge=True)
                return False
            address = self.client_address[0]
            if throttled(address) or not auth_slots.acquire(blocking=False):
                self._send(429, {'error': 'Service busy; retry shortly'})
                return False
            try:
                scheme, encoded = authorization.split(' ', 1)
                if scheme.lower() != 'basic' or len(encoded) > 4096:
                    raise ValueError
                username, password = base64.b64decode(encoded, validate=True).decode('utf-8').split(':', 1)
                iterations, salt, expected = users.get(username, next(iter(users.values())))
                actual = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, iterations)
                authenticated = hmac.compare_digest(actual, expected) and username in users
            except (ValueError, UnicodeError, binascii.Error):
                authenticated = False
            finally:
                auth_slots.release()
            record_auth(address, authenticated)
            if not authenticated:
                self._send(401, {'error': 'Authentication required'}, challenge=True)
            return authenticated

        def do_GET(self):
            if self.path == '/health':
                self._send(200, {'status': 'ok'})
                return
            if not self._guard():
                return
            entry = STATIC_FILES.get(self.path)
            if not entry:
                self._send(404, {'error': 'Not found'})
                return
            try:
                body = (PROJECT_ROOT / entry[0]).read_bytes()
            except OSError:
                self._send(503, {'error': 'Service unavailable'})
                return
            self._send(200, body, entry[1])

        def do_POST(self):
            if not self._guard():
                return
            operation = ROUTES.get(self.path)
            if operation is None:
                self._send(404, {'error': 'Not found'})
                return
            if self.headers.get('Content-Type', '').split(';', 1)[0].strip().lower() != 'application/json':
                self._send(415, {'error': 'Content-Type must be application/json'})
                return
            if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1:
                self._send(400, {'error': 'A single Content-Length is required'})
                return
            try:
                length = int(self.headers['Content-Length'])
            except (ValueError, TypeError):
                self._send(400, {'error': 'Invalid Content-Length'})
                return
            if not 1 <= length <= MAX_REQUEST_BYTES:
                self._send(413, {'error': 'Request body exceeds the supported size'})
                return
            try:
                raw = self.rfile.read(length)
                payload = json.loads(raw.decode('utf-8'), parse_constant=_invalid_constant)
                if not isinstance(payload, dict):
                    raise ValueError
                # Reject overflows such as 1e999 as well as NaN and Infinity.
                json.dumps(payload, allow_nan=False)
            except (ValueError, UnicodeError, TimeoutError, RecursionError):
                self._send(400, {'error': 'Request body is not a finite UTF-8 JSON object'})
                return
            acquired = False
            if self.path == '/api/optimizations':
                acquired = semaphore.acquire(blocking=False)
                if not acquired:
                    self._send(429, {'error': 'Optimizer busy; retry shortly'})
                    return
            connection = None
            try:
                connection = store.connect()
                result = operation(connection, payload)
                # Complete serialization before emitting a success response.
                body = json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8')
            except (RecommendationInputError, AllocationInputError) as error:
                self._send(400, {'error': str(error)})
            except Exception:
                self._send(503, {'error': 'Service unavailable'})
            else:
                self._send(200, body)
            finally:
                if connection is not None:
                    connection.close()
                if acquired:
                    semaphore.release()

    server = ThreadingHTTPServer((bind_host, port), Handler)
    server.daemon_threads = True
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', choices=['serve', 'hash-password'], default='serve')
    args = parser.parse_args(argv)
    if args.command == 'hash-password':
        password = getpass.getpass('Password: ')
        if password != getpass.getpass('Repeat password: '):
            parser.error('Passwords do not match')
        print(hash_password(password))
        return 0
    try:
        store = TursoStore(os.environ.get('TURSO_DATABASE_URL', ''), os.environ.get('TURSO_AUTH_TOKEN', ''))
        server = create_cloud_server(store, os.environ.get('UPS_USERS_JSON', ''),
            os.environ.get('UPS_ALLOWED_HOST') or os.environ.get('RENDER_EXTERNAL_HOSTNAME'),
            port=int(os.environ.get('PORT', '10000')), bind_host='0.0.0.0')
    except (ValueError, TypeError):
        parser.error('Invalid service configuration; check required environment variables')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
