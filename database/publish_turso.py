"""Explicitly publish a curated export to a dedicated Turso database.

Secrets are read from TURSO_DATABASE_URL and TURSO_AUTH_TOKEN (write token).
The hosted API should use a separate read-only token. No secrets are printed.
Protocol: https://github.com/tursodatabase/libsql/blob/main/docs/HTTP_V2_SPEC.md
"""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3

try:
    from .published_catalog import SCHEMA, FORMAT
    from .turso_store import TursoStore, TursoError, _value, MAX_MODELS
except ImportError:
    from published_catalog import SCHEMA, FORMAT
    from turso_store import TursoStore, TursoError, _value, MAX_MODELS


def publish(export_path, store):
    path = Path(export_path).resolve()
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as connection:
        metadata = connection.execute('SELECT key,value FROM catalog_meta').fetchall()
        models = connection.execute('SELECT product_id,manufacturer,model,sku,rating_w,rating_va,outlet_count,model_json FROM published_models ORDER BY product_id').fetchall()
    if dict(metadata).get('format') != FORMAT or len(models) > MAX_MODELS:
        raise ValueError('Unsupported curated export format or size')
    if not models:
        raise ValueError('Refusing to publish an empty curated export')
    try:
        declared_count = int(dict(metadata)['model_count'])
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError('Curated export model_count must equal its row count') from None
    if declared_count != len(models):
        raise ValueError('Curated export model_count must equal its row count')
    remote_tables = {row[0] for row in store.query(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    if remote_tables - {'published_models', 'catalog_meta'}:
        raise ValueError('Destination contains unrelated tables; use a dedicated database')
    if remote_tables:
        if remote_tables != {'published_models', 'catalog_meta'}:
            raise ValueError('Destination has an incomplete catalog schema')
        if dict(store.query('SELECT key,value FROM catalog_meta')).get('format') != FORMAT:
            raise ValueError('Destination is not a recognized curated catalog')

    statements = [('BEGIN IMMEDIATE', ())]
    statements.extend((ddl.replace('CREATE TABLE ', 'CREATE TABLE IF NOT EXISTS ', 1), ())
                      for ddl in SCHEMA.split(';') if ddl.strip())
    statements.extend([('DELETE FROM published_models', ()), ('DELETE FROM catalog_meta', ())])
    statements.extend(('INSERT INTO catalog_meta VALUES(?,?)', row) for row in metadata)
    statements.extend(('INSERT INTO published_models VALUES(?,?,?,?,?,?,?,?)', row) for row in models)
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
    if len(json.dumps(steps).encode()) > 8_000_000:
        raise ValueError('Curated export exceeds the bounded publisher request size')
    response = store.pipeline([{'type': 'batch', 'batch': {'steps': steps}}])[0]['result']
    if any(response.get('step_errors', [])) or response['step_results'][commit_index] is None:
        raise TursoError('Catalog refresh failed; transaction was rolled back')
    return {'models': len(models), 'includesPrices': dict(metadata).get('includes_prices') == 'true'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True, help='Curated export SQLite file')
    args = parser.parse_args()
    try:
        store = TursoStore(os.environ.get('TURSO_DATABASE_URL', ''),
                           os.environ.get('TURSO_AUTH_TOKEN', ''))
        print(json.dumps(publish(args.db, store)))
    except (ValueError, sqlite3.Error, TursoError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
