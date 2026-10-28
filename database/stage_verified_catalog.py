"""Apply exact manufacturer evidence to a new, private SQLite snapshot.

This local tool does not publish or contact Turso. The evidence manifest is a
versioned JSON file; neither credentials nor prices are printed by the CLI.
"""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
from urllib.parse import unquote, urlsplit

try:
    from .published_catalog import FORMAT, MODEL_COLUMNS, SCHEMA, SUPPORTED_FORMATS
    from .publish_turso import BASE_SNAPSHOT_KEY, snapshot_fingerprint
    from .verified_evidence import (_configuration, _crosswalk, _group, _key,
                                    _positive_number, _product_key, _source,
                                    apply_verified_evidence)
except ImportError:
    from published_catalog import FORMAT, MODEL_COLUMNS, SCHEMA, SUPPORTED_FORMATS
    from publish_turso import BASE_SNAPSHOT_KEY, snapshot_fingerprint
    from verified_evidence import (_configuration, _crosswalk, _group, _key,
                                   _positive_number, _product_key, _source,
                                   apply_verified_evidence)

MAX_MANIFEST_BYTES = 8_000_000
EXPECTED_TABLES = {'published_models', 'catalog_meta'}
IDENTITY_FIELDS = ('id', 'manufacturer', 'model', 'sku', 'product_type')
PROTECTED_FIELDS = ('prices', 'estimated_cost', 'evidence')
_SENSITIVE_KEY_PARTS = ('secret', 'token', 'password', 'passwd', 'passphrase', 'pwd', 'email', 'price',
                        'cost', 'amount', 'credential', 'authorization', 'apikey',
                        'privatekey')
_URL = re.compile(r'''(?i)\b(?:(?:https?|ftps?|file|libsql)://|www\.)[^\s<>"']+''')
_WINDOWS_PATH = re.compile(r'(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|\\{2,}[^\\/\s]+[\\/]|//[^/\s]+/)')
_WINDOWS_ENV_PATH = re.compile(
    r'(?i)(?:%(?:USERPROFILE|APPDATA|LOCALAPPDATA|HOMEPATH|HOMEDRIVE|TEMP|TMP)%|\$env:(?:USERPROFILE|APPDATA|LOCALAPPDATA|HOMEPATH|TEMP|TMP)|\$\{env:(?:USERPROFILE|APPDATA|LOCALAPPDATA|HOMEPATH|TEMP|TMP)\})[\\/]')
_PRIVATE_PATH = re.compile(
    r'(?i)(?<![A-Za-z0-9])(?:~[\\/]|/(?:home|users|mnt|media|tmp|var|private|root|workspace|workspaces|data|appdata|etc)(?:/|$)|(?:private|secret|confidential|customer|client)[\\/])')
_EMAIL = re.compile(r'(?i)\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b')
_SECRET_ASSIGNMENT = re.compile(
    r'(?i)\b(?:password|passwd|token|secret|api[\s_-]*key|authorization|credentials?)\b\s*[:=]\s*\S+')
_BEARER = re.compile(r'(?i)\bbearer\s+[a-z0-9._~+/=-]{5,}')
_CREDENTIAL = re.compile(
    r'(?i)\b(?:sk-(?:proj-)?[a-z0-9_-]{12,}|gh[pousr]_[a-z0-9_-]{12,}|github_pat_[a-z0-9_]{12,}|eyj[a-z0-9_-]{15,}\.[a-z0-9_-]{10,}\.[a-z0-9_-]{10,})\b')


def _reject_constant(_value):
    raise ValueError('Non-finite JSON values are not allowed')


def _private_text(value):
    """Identify common private paths, credentials and decorated URLs."""
    decoded = unquote(value)
    for match in _URL.finditer(decoded):
        url = match.group(0)
        try:
            parsed = urlsplit('https://' + url if url.casefold().startswith('www.') else url)
            if (parsed.scheme.casefold() == 'file' or parsed.username is not None
                    or parsed.password is not None or '?' in url or '#' in url
                    or not parsed.hostname):
                return True
        except ValueError:
            return True
    without_urls = _URL.sub(' ', decoded)
    return bool(_WINDOWS_PATH.search(without_urls) or _WINDOWS_ENV_PATH.search(without_urls)
                or _PRIVATE_PATH.search(without_urls)
                or _EMAIL.search(without_urls) or _SECRET_ASSIGNMENT.search(without_urls)
                or _BEARER.search(without_urls) or _CREDENTIAL.search(without_urls))


def _validate_manifest_privacy(value):
    """Reject private material recursively without reporting rejected content."""
    active = set()

    def visit(item, depth):
        if depth > 64:
            raise ValueError('Evidence manifest contains disallowed private content')
        if isinstance(item, (dict, list)):
            identifier = id(item)
            if identifier in active:
                raise ValueError('Evidence manifest contains disallowed private content')
            active.add(identifier)
            try:
                if isinstance(item, dict):
                    for key, child in item.items():
                        if not isinstance(key, str):
                            raise ValueError('Evidence manifest contains disallowed private content')
                        normalized = re.sub(r'[^a-z0-9]', '', key.casefold())
                        if any(part in normalized for part in _SENSITIVE_KEY_PARTS) or _private_text(key):
                            raise ValueError('Evidence manifest contains disallowed private content')
                        visit(child, depth + 1)
                else:
                    for child in item:
                        visit(child, depth + 1)
            finally:
                active.remove(identifier)
        elif isinstance(item, str):
            if _private_text(item):
                raise ValueError('Evidence manifest contains disallowed private content')
        elif item is None or isinstance(item, (bool, int)):
            return
        elif isinstance(item, float) and math.isfinite(item):
            return
        else:
            raise ValueError('Evidence manifest contains disallowed private content')

    visit(value, 0)


def _read_snapshot(path, *, with_fingerprint=False):
    """Read a consistent snapshot through SQLite's read-only URI mode."""
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute('PRAGMA query_only=ON')
            connection.execute('BEGIN')
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if tables != EXPECTED_TABLES:
                raise ValueError('Snapshot must contain only the published catalog tables')
            for table, columns in (('published_models', MODEL_COLUMNS),
                                   ('catalog_meta', ('key', 'value'))):
                actual = tuple(row[1] for row in connection.execute('PRAGMA table_info(' + table + ')'))
                if actual != columns:
                    raise ValueError('Unsupported snapshot schema')
            metadata_rows = [tuple(row) for row in connection.execute('SELECT key,value FROM catalog_meta')]
            metadata = dict(metadata_rows)
            if metadata.get('format') not in SUPPORTED_FORMATS:
                raise ValueError('Unsupported catalog format')
            count = metadata.get('model_count')
            if not isinstance(count, str) or not re.fullmatch(r'[0-9]+', count):
                raise ValueError('Invalid snapshot model_count')
            rows = connection.execute(
                'SELECT product_id,manufacturer,model,sku,rating_w,rating_va,outlet_count,model_json '
                'FROM published_models ORDER BY product_id').fetchall()
            if int(count) != len(rows):
                raise ValueError('Snapshot model_count does not match its rows')
            inventory = []
            for row in rows:
                if not isinstance(row['model_json'], str):
                    raise ValueError('Invalid model JSON')
                try:
                    model = json.loads(row['model_json'], parse_constant=_reject_constant)
                except (TypeError, ValueError, RecursionError):
                    raise ValueError('Invalid model JSON') from None
                if not isinstance(model, dict):
                    raise ValueError('Invalid model JSON')
                for column, field in (('product_id', 'id'), ('manufacturer', 'manufacturer'),
                                      ('model', 'model'), ('sku', 'sku'),
                                      ('rating_w', 'rating_w'), ('rating_va', 'rating_va'),
                                      ('outlet_count', 'outlet_count')):
                    if field not in model or model[field] != row[column]:
                        raise ValueError('Snapshot columns disagree with model JSON')
                inventory.append(model)
            fingerprint = snapshot_fingerprint(metadata_rows, [tuple(row) for row in rows])
            connection.rollback()
            return (metadata, inventory, fingerprint) if with_fingerprint else (metadata, inventory)
    except sqlite3.Error:
        raise ValueError('Cannot read the catalog snapshot') from None


def _validate_records(inventory, records):
    """Reject ambiguous or silently skipped records before creating a file."""
    if not isinstance(records, list):
        raise ValueError('Evidence records must be a list')
    products = {}
    for product in inventory:
        key = _product_key(product)
        if key is not None:
            products.setdefault(key, []).append(product)
    seen = set()
    for record in records:
        key = _key(record)
        if key is None:
            raise ValueError('Invalid evidence identity or source')
        if key in seen:
            raise ValueError('Duplicate evidence identity')
        seen.add(key)
        matches = products.get(key, [])
        if len(matches) != 1:
            raise ValueError('Evidence identity is missing or ambiguous')
        product = matches[0]
        maker = record['manufacturer']
        if (record['catalogSku'] is not None and record['catalogSku'] != record['manufacturerSku']
                and not _crosswalk(record.get('crosswalkSource'), product, maker)):
            raise ValueError('Evidence crosswalk is missing or does not match this product')
        rating_fields = ('ratingWatts', 'ratingVa')
        if any(field in record for field in rating_fields) and not _source(record.get('ratingSource'), maker):
            raise ValueError('Invalid manufacturer rating source')
        for field in rating_fields:
            if field in record and _positive_number(record[field]) is None:
                raise ValueError('Invalid manufacturer rating')
        groups = record.get('outletGroups', [])
        if not isinstance(groups, list) or any(_group(group, maker) is None for group in groups):
            raise ValueError('Invalid manufacturer outlet group')
        configurations = record.get('batteryConfigurations', [])
        rated_w = _positive_number(record.get('ratingWatts'))
        if rated_w is None and product.get('rating_w_verified') is True:
            rated_w = _positive_number(product.get('rating_w'))
        if (not isinstance(configurations, list)
                or any(_configuration(config, maker, rated_w) is None for config in configurations)):
            raise ValueError('Invalid manufacturer battery configuration')
        conflicts = record.get('conflicts', [])
        if (not isinstance(conflicts, list)
                or any(not isinstance(item, dict) or not isinstance(item.get('field'), str)
                       or not item['field'].strip() for item in conflicts)):
            raise ValueError('Invalid manufacturer conflict')


def _check_preservation(before, after):
    """Check private commercial values and opaque references survive overlay."""
    if len(before) != len(after):
        raise ValueError('Evidence overlay changed the number of products')
    for old, new in zip(before, after):
        for field in IDENTITY_FIELDS + PROTECTED_FIELDS:
            if (field in old) != (field in new) or old.get(field) != new.get(field):
                raise ValueError('Evidence overlay changed protected catalog data')
        prior = old.get('manufacturer_conflicts')
        if isinstance(prior, list):
            current = new.get('manufacturer_conflicts')
            if not isinstance(current, list) or current[:len(prior)] != prior:
                raise ValueError('Evidence overlay changed prior conflict evidence')
        elif 'manufacturer_conflicts' in old and prior != new.get('manufacturer_conflicts'):
            raise ValueError('Evidence overlay changed prior conflict evidence')
        old_crosswalk = old.get('identity_crosswalk')
        if (old_crosswalk is not None and new.get('identity_crosswalk') != old_crosswalk
                and old_crosswalk not in (new.get('previous_identity_crosswalk') or [])):
            raise ValueError('Evidence overlay lost a prior identity crosswalk')


def _same_created_file(path, identity):
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    return (stat.S_ISREG(info.st_mode) and identity[1] != 0
            and (info.st_dev, info.st_ino) == identity)


def stage_catalog(source_path, output_path, records):
    """Create a new v2 snapshot; leave the source and existing paths untouched."""
    source = Path(source_path).resolve(strict=True)
    if not source.is_file():
        raise ValueError('Source must be a catalog file')
    requested_output = Path(output_path)
    if requested_output.name in {'', '.', '..'}:
        raise ValueError('Output must name a new file')
    # Resolve the directory, but never follow the final component. A dangling
    # symlink at the requested output is still an existing path, not permission
    # to create the file to which it happens to point.
    output = requested_output.parent.resolve(strict=True) / requested_output.name
    if source == output:
        raise ValueError('Source and output must be different files')
    if os.path.lexists(output):
        raise FileExistsError('Output already exists')

    _validate_manifest_privacy(records)
    metadata, inventory, fingerprint = _read_snapshot(source, with_fingerprint=True)
    base = metadata.get(BASE_SNAPSHOT_KEY, fingerprint)
    if not isinstance(base, str) or not re.fullmatch(r'[0-9a-f]{64}', base):
        raise ValueError('Invalid catalog base fingerprint')
    _validate_records(inventory, records)
    enriched = apply_verified_evidence(inventory, records)
    _check_preservation(inventory, enriched)

    # Validate JSON before the output exists; retain complete input values rather
    # than re-projecting and potentially dropping commercial or evidence fields.
    model_rows = []
    for product in enriched:
        payload = json.dumps(product, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        model_rows.append((product['id'], product.get('manufacturer'), product.get('model'),
                           product.get('sku'), product.get('rating_w'), product.get('rating_va'),
                           product.get('outlet_count'), payload))
    metadata['format'] = FORMAT
    metadata['generated_at'] = datetime.now(timezone.utc).isoformat()
    metadata['model_count'] = str(len(model_rows))
    metadata[BASE_SNAPSHOT_KEY] = base

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0)
    descriptor = os.open(str(output), flags, 0o600)
    try:
        info = os.fstat(descriptor)
        identity = (info.st_dev, info.st_ino)
    finally:
        os.close(descriptor)
    try:
        with closing(sqlite3.connect(output)) as connection:
            if not _same_created_file(output, identity):
                raise ValueError('Output was replaced during staging')
            connection.executescript(SCHEMA)
            with connection:
                connection.executemany('INSERT INTO catalog_meta VALUES(?,?)', metadata.items())
                connection.executemany('INSERT INTO published_models VALUES(?,?,?,?,?,?,?,?)', model_rows)
        if not _same_created_file(output, identity):
            raise ValueError('Output was replaced during staging')
    except BaseException:
        if _same_created_file(output, identity):
            try:
                output.unlink()
            except OSError:
                pass
        raise
    return {'models': len(model_rows), 'matchedRecords': len(records), 'filename': output.name}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True, help='Read-only private catalog snapshot')
    parser.add_argument('--output', type=Path, required=True, help='New private staged SQLite file')
    parser.add_argument('--manifest', type=Path, required=True, help='Private versioned evidence JSON')
    args = parser.parse_args()
    try:
        if args.manifest.stat().st_size > MAX_MANIFEST_BYTES:
            raise ValueError('Evidence manifest exceeds size limit')
        with args.manifest.open(encoding='utf-8') as handle:
            manifest = json.load(handle, parse_constant=_reject_constant)
        _validate_manifest_privacy(manifest)
        if (not isinstance(manifest, dict) or type(manifest.get('version')) is not int
                or manifest['version'] != 1 or not isinstance(manifest.get('records'), list)):
            raise ValueError('Unsupported evidence manifest')
        result = stage_catalog(args.source, args.output, manifest['records'])
    except (OSError, ValueError, TypeError, sqlite3.Error, RecursionError):
        parser.exit(1, 'Catalog staging failed; check the snapshot, output path, and evidence manifest.\n')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
