"""Explicitly publish a curated export to a dedicated Turso database.

Secrets are read from TURSO_DATABASE_URL and TURSO_AUTH_TOKEN (write token).
The hosted API should use a separate read-only token. No secrets are printed.
Protocol: https://github.com/tursodatabase/libsql/blob/main/docs/HTTP_V2_SPEC.md
"""

import argparse
from contextlib import closing
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys

try:
    from .published_catalog import SCHEMA, FORMAT, SUPPORTED_FORMATS
    from .turso_store import TursoStore, TursoError, _value, MAX_MODELS
except ImportError:
    from published_catalog import SCHEMA, FORMAT, SUPPORTED_FORMATS
    from turso_store import TursoStore, TursoError, _value, MAX_MODELS


MAX_BATCH_BYTES = 8_000_000
_MODEL_COLUMNS = ('product_id', 'manufacturer', 'model', 'sku', 'rating_w',
                  'rating_va', 'outlet_count', 'model_json')
_EXPECTED_SCHEMA = {
    'published_models': [('product_id', 'INTEGER', 0, 1), ('manufacturer', 'TEXT', 0, 0),
                         ('model', 'TEXT', 0, 0), ('sku', 'TEXT', 0, 0), ('rating_w', 'REAL', 0, 0),
                         ('rating_va', 'REAL', 0, 0), ('outlet_count', 'INTEGER', 0, 0),
                         ('model_json', 'TEXT', 1, 0)],
    'catalog_meta': [('key', 'TEXT', 0, 1), ('value', 'TEXT', 1, 0)],
}
_GUARD_TABLE = '_ups_publish_snapshot_guard'
BASE_SNAPSHOT_KEY = 'base_snapshot_sha256'


def _catalog_model(row):
    """Validate the JSON consumed by the API without exposing its contents."""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate catalog JSON key')
            result[key] = value
        return result

    def invalid_constant(_value):
        raise ValueError('Non-finite catalog JSON value')

    try:
        model = json.loads(row[7], object_pairs_hook=unique_object,
                           parse_constant=invalid_constant)
        if not isinstance(model, dict):
            raise ValueError('Catalog model must be an object')
        required = ('manufacturer_id', 'product_type', 'description', 'non_ups_reason',
                    'external_battery_supported', 'external_battery_model',
                    'max_external_battery_packs', 'warnings', 'prices', 'estimated_cost',
                    'evidence', 'runtime_curve')
        if any(key not in model for key in required):
            raise ValueError('Catalog model is missing application fields')
        for index, key in enumerate(('id', 'manufacturer', 'model', 'sku',
                                     'rating_w', 'rating_va', 'outlet_count')):
            value = model[key]
            if isinstance(value, bool) or value != row[index]:
                raise ValueError('Catalog model does not match indexed fields')
        prices = model.get('prices', [])
        evidence = model.get('evidence', [])
        chosen = model.get('estimated_cost')
        expansion = model['max_external_battery_packs']
        curve = model['runtime_curve']
        lifecycle = model.get('lifecycle_observations')
        if (not isinstance(prices, list) or not isinstance(evidence, list)
                or not all(isinstance(item, dict) for item in evidence)
                or any(item.get(key) is not None and not isinstance(item.get(key), str)
                       for item in evidence for key in ('path', 'locator'))
                or not isinstance(model['warnings'], list)
                or not all(isinstance(item, str) for item in model['warnings'])
                or (lifecycle is not None and (not isinstance(lifecycle, list)
                    or not all(isinstance(item, dict) for item in lifecycle)))
                or (model['external_battery_supported'] is not None
                    and type(model['external_battery_supported']) is not bool)
                or (expansion is not None and (type(expansion) is not int or expansion < 0))
                or (curve is not None and (not isinstance(curve, dict)
                    or not isinstance(curve.get('points'), list)
                    or not all(isinstance(point, dict) for point in curve['points'])
                    or not isinstance(curve.get('source'), dict)))
                or chosen is not None and not all(key in chosen for key in
                    ('amount', 'currency', 'vatTreatment', 'kind'))):
            raise ValueError('Invalid catalog facts')
        if chosen is not None and any(chosen[key] is not None and not isinstance(chosen[key], str)
                                      for key in ('currency', 'vatTreatment', 'kind')):
            raise ValueError('Invalid catalog cost metadata')
        for offer in prices + ([chosen] if chosen is not None else []):
            if not isinstance(offer, dict) or isinstance(offer.get('amount'), bool):
                raise ValueError('Invalid catalog offer')
            amount = Decimal(offer['amount'])
            floating_amount = float(amount)
            # Leave ample room for fleet multiplication and sums during JSON output.
            if (not amount.is_finite() or not math.isfinite(floating_amount)
                    or not 0 < floating_amount <= sys.float_info.max / 10000):
                raise ValueError('Invalid catalog offer')
        return model
    except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
        raise ValueError('Catalog model JSON is invalid or inconsistent') from None


def _preserve_existing_catalog(expected_models, staged_models):
    """An ordinary refresh may add models but cannot silently replace old facts."""
    staged_by_id = {row[0]: row for row in staged_models}
    for before in expected_models:
        after = staged_by_id.get(before[0])
        if after is None or before[1:4] != after[1:4]:
            raise ValueError('A catalog refresh must retain existing model identities')
        old_model, new_model = _catalog_model(before), _catalog_model(after)
        for key, default in (('prices', []), ('evidence', []), ('estimated_cost', None)):
            if old_model.get(key, default) != new_model.get(key, default):
                raise ValueError('A catalog refresh must retain existing price and source evidence')
        # Verified sources may change only if their former value remains in history.
        for field in ('identity_source', 'identity_crosswalk', 'rating_w_source',
                      'rating_va_source', 'manufacturer_outlet_source', 'runtime_curve',
                      'outlet_groups', 'battery_configurations'):
            previous = old_model.get(field)
            current = new_model.get(field)
            history = new_model.get('previous_' + field) or []
            if not isinstance(history, list):
                raise ValueError('Catalog evidence history is invalid')
            if isinstance(previous, list):
                if not isinstance(current, list) or any(
                        item not in current and item not in history for item in previous):
                    raise ValueError('Catalog refresh lost manufacturer evidence')
            elif previous is not None and previous != current and previous not in history:
                raise ValueError('Catalog refresh lost manufacturer evidence')
        for field in ('manufacturer_conflicts', 'lifecycle_observations'):
            previous = old_model.get(field) or []
            current = new_model.get(field) or []
            if (not isinstance(previous, list) or not isinstance(current, list)
                    or any(item not in current for item in previous)):
                raise ValueError('Catalog refresh lost conflicts or lifecycle evidence')
        for field, previous in old_model.items():
            if not field.startswith('previous_') or previous is None:
                continue
            current = new_model.get(field)
            if isinstance(previous, list):
                if not isinstance(current, list) or any(item not in current for item in previous):
                    raise ValueError('Catalog refresh lost evidence history')
            elif previous != current:
                raise ValueError('Catalog refresh lost evidence history')
        prior_ratings = old_model.get('original_source_ratings') or {}
        current_ratings = new_model.get('original_source_ratings') or {}
        if (not isinstance(prior_ratings, dict) or not isinstance(current_ratings, dict)
                or any(current_ratings.get(key) != value for key, value in prior_ratings.items())):
            raise ValueError('Catalog refresh lost original rating evidence')


def snapshot_fingerprint(metadata, models):
    """Hash every SQLite value with its type, in a deterministic row order."""
    def tagged(value):
        if value is None:
            return ['null']
        if type(value) is int:
            return ['integer', str(value)]
        if type(value) is float and math.isfinite(value):
            return ['real', value.hex()]
        if isinstance(value, str):
            return ['text', value]
        raise ValueError('Snapshot contains an unsupported value type')

    digest = hashlib.sha256(b'ups-catalog-snapshot-v1\n')
    for table, rows in (('catalog_meta', sorted(metadata, key=lambda row: row[0])),
                        ('published_models', sorted(models, key=lambda row: row[0]))):
        digest.update((table + '\n').encode('ascii'))
        for row in rows:
            encoded = json.dumps([tagged(value) for value in row], ensure_ascii=False,
                                 allow_nan=False, separators=(',', ':')).encode('utf-8')
            digest.update(str(len(encoded)).encode('ascii') + b':' + encoded + b'\n')
    return digest.hexdigest()


def _read_catalog(path, *, expected):
    """Validate a read-only export/snapshot without exposing row contents."""
    path = Path(path).resolve()
    if not path.is_file():
        raise ValueError('Expected snapshot file is unavailable' if expected
                         else 'Curated export file is unavailable')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as connection:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*'")}
        if tables != set(_EXPECTED_SCHEMA):
            raise ValueError('Invalid expected snapshot schema' if expected else 'Invalid curated export schema')
        for table, columns in _EXPECTED_SCHEMA.items():
            info = connection.execute(f'PRAGMA table_info({table})').fetchall()
            if ([(row[1], row[2].upper(), row[3], row[5]) for row in info] != columns
                    or any(row[4] is not None for row in info)):
                raise ValueError('Invalid expected snapshot schema' if expected else 'Invalid curated export schema')
        metadata = connection.execute('SELECT key,value FROM catalog_meta ORDER BY key').fetchall()
        models = connection.execute(
            'SELECT product_id,manufacturer,model,sku,rating_w,rating_va,outlet_count,model_json '
            'FROM published_models ORDER BY product_id').fetchall()
        connection.rollback()
    if any(not isinstance(key, str) or not key or not isinstance(value, str) for key, value in metadata):
        raise ValueError('Catalog metadata is invalid')
    versions = SUPPORTED_FORMATS if expected else {FORMAT}
    if dict(metadata).get('format') not in versions or len(models) > MAX_MODELS:
        raise ValueError('Unsupported expected snapshot format or size' if expected
                         else 'Unsupported curated export format or size')
    if not models and not expected:
        raise ValueError('Refusing to publish an empty curated export')
    try:
        declared_count = int(dict(metadata)['model_count'])
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError('Curated export model_count must equal its row count') from None
    if declared_count != len(models) or declared_count < 0:
        raise ValueError('Curated export model_count must equal its row count')
    for row in models:
        if (not isinstance(row[0], int) or not isinstance(row[7], str)
                or any(value is not None and not isinstance(value, str) for value in row[1:4])
                or any(value is not None and (not isinstance(value, (int, float))
                                              or not math.isfinite(value)) for value in row[4:6])
                or row[6] is not None and not isinstance(row[6], int)):
            raise ValueError('Catalog row values are invalid')
        _catalog_model(row)
    return metadata, models


def _guard_statements(expected_metadata, expected_models):
    """Fail with a duplicate-key violation if the locked destination differs."""
    count_guard = (
        f'INSERT INTO {_GUARD_TABLE}(id) SELECT 1 WHERE '
        '(SELECT count(*) FROM catalog_meta) != ? OR '
        '(SELECT count(*) FROM published_models) != ? OR '
        "EXISTS (SELECT 1 FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' "
        "AND (type != 'table' OR name NOT IN "
        "('catalog_meta','published_models','_ups_publish_snapshot_guard')))"
    )
    statements = [(count_guard, (len(expected_metadata), len(expected_models)))]
    meta_guard = (f'INSERT INTO {_GUARD_TABLE}(id) SELECT 1 WHERE NOT EXISTS '
                  '(SELECT 1 FROM catalog_meta WHERE key COLLATE BINARY IS ? '
                  "AND value COLLATE BINARY IS ? AND typeof(key)='text' AND typeof(value)='text')")
    statements.extend((meta_guard, row) for row in expected_metadata)
    text_columns = {'manufacturer', 'model', 'sku', 'model_json'}
    comparisons = ' AND '.join(
        f'{column} COLLATE BINARY IS ? AND typeof({column}) = ?' if column in text_columns
        else f'{column} IS ? AND typeof({column}) = ?' for column in _MODEL_COLUMNS)
    model_guard = (f'INSERT INTO {_GUARD_TABLE}(id) SELECT 1 WHERE NOT EXISTS '
                   f'(SELECT 1 FROM published_models WHERE {comparisons})')
    for row in expected_models:
        args = []
        for value in row:
            kind = ('null' if value is None else 'integer' if isinstance(value, int)
                    else 'real' if isinstance(value, float) else 'text')
            args.extend((value, kind))
        statements.append((model_guard, tuple(args)))
    return statements


def publish(export_path, store, expected_snapshot_path=None):
    metadata, models = _read_catalog(export_path, expected=False)
    expected_catalog = (_read_catalog(expected_snapshot_path, expected=True)
                        if expected_snapshot_path is not None else None)
    if expected_catalog is not None:
        recorded = dict(metadata).get(BASE_SNAPSHOT_KEY)
        if (not isinstance(recorded, str) or not re.fullmatch(r'[0-9a-f]{64}', recorded)
                or not hmac.compare_digest(recorded, snapshot_fingerprint(*expected_catalog))):
            raise ValueError('Staged catalog base fingerprint does not match the expected snapshot')
        _preserve_existing_catalog(expected_catalog[1], models)
    remote_tables = {row[0] for row in store.query(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*'")}
    if remote_tables - {'published_models', 'catalog_meta'}:
        raise ValueError('Destination contains unrelated tables; use a dedicated database')
    if remote_tables:
        if remote_tables != {'published_models', 'catalog_meta'}:
            raise ValueError('Destination has an incomplete catalog schema')
        remote_metadata = store.query('SELECT key,value FROM catalog_meta')
        if remote_metadata:
            if dict(remote_metadata).get('format') not in SUPPORTED_FORMATS:
                raise ValueError('Destination is not a recognized curated catalog')
            if expected_catalog is None:
                raise ValueError('An expected snapshot is required for an existing catalog')

    statements = [('BEGIN IMMEDIATE', ())]
    statements.extend((ddl.replace('CREATE TABLE ', 'CREATE TABLE IF NOT EXISTS ', 1), ())
                      for ddl in SCHEMA.split(';') if ddl.strip())
    statements.extend([(f'CREATE TABLE {_GUARD_TABLE}(id INTEGER PRIMARY KEY)', ()),
                       (f'INSERT INTO {_GUARD_TABLE}(id) VALUES(1)', ())])
    statements.extend(_guard_statements(*(expected_catalog or ([], []))))
    statements.extend([('DELETE FROM published_models', ()), ('DELETE FROM catalog_meta', ())])
    # This is a staging precondition, not catalog data. Persisting it would
    # make a later backup look like a stage still based on an older snapshot.
    published_metadata = [row for row in metadata if row[0] != BASE_SNAPSHOT_KEY]
    statements.extend(('INSERT INTO catalog_meta VALUES(?,?)', row) for row in published_metadata)
    statements.extend(('INSERT INTO published_models VALUES(?,?,?,?,?,?,?,?)', row) for row in models)
    # Check the complete written contents again while still holding the write lock.
    statements.extend(_guard_statements(published_metadata, models))
    statements.append((f'DROP TABLE {_GUARD_TABLE}', ()))
    steps = []
    for index, (sql, args) in enumerate(statements):
        step = {'stmt': {'sql': sql, 'args': [_value(value) for value in args], 'want_rows': False}}
        if index:
            step['condition'] = {'type': 'ok', 'step': index - 1}
        steps.append(step)
    commit_index = len(steps)
    steps.append({'stmt': {'sql': 'COMMIT', 'want_rows': False},
                  'condition': {'type': 'ok', 'step': commit_index - 1}})
    steps.append({'stmt': {'sql': 'ROLLBACK', 'want_rows': False},
                  'condition': {'type': 'not', 'cond': {'type': 'ok', 'step': commit_index}}})
    request = {'type': 'batch', 'batch': {'steps': steps}}
    body = {'requests': [request, {'type': 'close'}]}
    if len(json.dumps(body, allow_nan=False).encode()) > MAX_BATCH_BYTES:
        raise ValueError('Curated export exceeds the bounded publisher request size')
    response = store.pipeline([request])[0]['result']
    results, errors = response.get('step_results'), response.get('step_errors')
    if (not isinstance(results, list) or not isinstance(errors, list)
            or len(results) != len(steps) or len(errors) != len(steps)):
        raise TursoError('Catalog refresh status is unknown')
    if any(errors) or results[commit_index] is None:
        if results[-1] is None or errors[-1] is not None:
            raise TursoError('Catalog refresh failed; rollback status is unknown')
        raise TursoError('Catalog refresh failed; transaction was rolled back')
    return {'models': len(models), 'includesPrices': dict(metadata).get('includes_prices') == 'true'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True, help='Curated export SQLite file')
    parser.add_argument('--expected-source', type=Path,
                        help='Read-only snapshot of the existing remote catalog')
    args = parser.parse_args()
    try:
        store = TursoStore(os.environ.get('TURSO_DATABASE_URL', ''),
                           os.environ.get('TURSO_AUTH_TOKEN', ''))
        print(json.dumps(publish(args.db, store, expected_snapshot_path=args.expected_source)))
    except (ValueError, sqlite3.Error, TursoError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
