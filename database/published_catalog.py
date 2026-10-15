"""Export the derived UPS facts without publishing extraction documents or paths."""

import argparse
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

SCHEMA = '''CREATE TABLE published_models(product_id INTEGER PRIMARY KEY,
manufacturer TEXT,model TEXT,sku TEXT,rating_w REAL,rating_va REAL,
outlet_count INTEGER,model_json TEXT NOT NULL);
CREATE TABLE catalog_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);'''
MODEL_COLUMNS = ('product_id', 'manufacturer', 'model', 'sku', 'rating_w',
                 'rating_va', 'outlet_count', 'model_json')
FORMAT = 'ups-curated-v1'


def _source(reference):
    if not isinstance(reference, dict):
        return None
    identity = json.dumps(reference, sort_keys=True, ensure_ascii=False)
    result = {'sourceId': hashlib.sha256(identity.encode()).hexdigest()[:24]}
    # Legacy engine evidence deduplication expects a path key. This is an opaque
    # reference, never a filesystem path or original document filename.
    result['path'] = 'source:' + result['sourceId']
    parsed = urlsplit(str(reference.get('url') or ''))
    if (parsed.scheme == 'https' and parsed.hostname and not parsed.username
            and not parsed.password and not parsed.query and not parsed.fragment):
        result['url'] = parsed.geturl()
        # Titles from manufacturer overlay are public document titles.
        if reference.get('title'):
            result['title'] = str(reference['title'])[:300]
        if reference.get('publicationYear'):
            result['publicationYear'] = reference['publicationYear']
        result.pop('path')
    return result


def project_model(product, include_prices=False):
    keys = ('id', 'manufacturer_id', 'manufacturer', 'model', 'sku', 'product_type',
            'rating_w', 'rating_va', 'outlet_count', 'external_battery_supported',
            'external_battery_model', 'max_external_battery_packs', 'warnings')
    result = {key: product.get(key) for key in keys}
    result.update(description=None, non_ups_reason=None, prices=[], estimated_cost=None)
    result['evidence'] = []
    for evidence in product.get('evidence', []):
        result['evidence'].append({**_source(evidence), **{
            key: bool(evidence.get(key)) for key in ('ratingVaEvidence', 'ratingWEvidence',
                'runtimeEvidence', 'outletEvidence', 'expansionEvidence')}})
    curve = product.get('runtime_curve')
    result['runtime_curve'] = ({key: curve[key] for key in
        ('points', 'batteryConfiguration', 'conditions') if key in curve} if curve else None)
    if curve:
        result['runtime_curve']['source'] = _source(curve.get('source'))
    if product.get('manufacturer_outlet_source'):
        result['manufacturer_outlet_source'] = _source(product['manufacturer_outlet_source'])
    if include_prices:
        for offer in product.get('prices', []):
            clean = {key: offer.get(key) for key in ('amount', 'currency', 'vatTreatment',
                'sourceDate', 'priceEffectiveFrom', 'priceEffectiveTo', 'priceType', 'kind')}
            clean['source'] = _source(offer.get('source'))
            result['prices'].append(clean)
        chosen = product.get('estimated_cost')
        if chosen:
            result['estimated_cost'] = {key: chosen.get(key) for key in
                ('amount', 'currency', 'vatTreatment', 'sourceDate', 'priceEffectiveFrom',
                 'priceEffectiveTo', 'priceType', 'kind')}
            result['estimated_cost']['source'] = _source(chosen.get('source'))
    else:
        result['warnings'] = [warning for warning in (result['warnings'] or [])
                              if 'price' not in warning.casefold()]
    return result


def load_published_inventory(connection):
    models = []
    for row in connection.execute('SELECT model_json FROM published_models ORDER BY product_id'):
        model = json.loads(row[0])
        for offer in model.get('prices', []):
            offer['decimal'] = Decimal(offer['amount'])
        if model.get('estimated_cost'):
            model['estimated_cost']['decimal'] = Decimal(model['estimated_cost']['amount'])
        models.append(model)
    return models


def export_catalog(source_db, output_db, include_prices=False, include_incomplete=False):
    try:
        from .recommendation_engine import _build_inventory, _apply_manufacturer_curves, _read_curve_catalog
    except ImportError:
        from recommendation_engine import _build_inventory, _apply_manufacturer_curves, _read_curve_catalog
    source, output = Path(source_db).resolve(), Path(output_db).resolve()
    if source == output:
        raise ValueError('Source and export paths must differ')
    if output.exists():
        raise ValueError('Export path already exists; choose a new output path')
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        inventory = _build_inventory(connection)
        _apply_manufacturer_curves(inventory, _read_curve_catalog(None))
    real_ups = [item for item in inventory if not item.get('non_ups_reason')]
    missing_capacity_count = sum(not (item.get('rating_w') and item.get('rating_va'))
                                 for item in real_ups)
    inventory = [item for item in real_ups if
                 include_incomplete or (item.get('rating_w') and item.get('rating_va'))]
    output.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(output)) as connection, connection:
        connection.executescript(SCHEMA)
        for product in inventory:
            model = project_model(product, include_prices)
            connection.execute('INSERT INTO published_models VALUES(?,?,?,?,?,?,?,?)',
                (model['id'], model['manufacturer'], model['model'], model['sku'],
                 model['rating_w'], model['rating_va'], model['outlet_count'],
                 json.dumps(model, ensure_ascii=False, allow_nan=False)))
        connection.executemany('INSERT INTO catalog_meta VALUES(?,?)', [
            ('format', FORMAT), ('generated_at', datetime.now(timezone.utc).isoformat()),
            ('includes_prices', str(bool(include_prices)).lower()),
            ('includes_incomplete', str(bool(include_incomplete)).lower()),
            ('original_model_count', str(len(real_ups))),
            ('omitted_missing_capacity_count', str(0 if include_incomplete else missing_capacity_count)),
            ('model_count', str(len(inventory)))])
    return {'models': len(inventory), 'includesPrices': bool(include_prices),
            'sourceRealUpsCount': len(real_ups),
            'omittedMissingCapacityCount': 0 if include_incomplete else missing_capacity_count,
            'bytes': output.stat().st_size}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--include-prices', action='store_true')
    parser.add_argument('--include-incomplete', action='store_true')
    args = parser.parse_args()
    print(json.dumps(export_catalog(args.db, args.output, args.include_prices,
                                   args.include_incomplete)))


if __name__ == '__main__':
    main()
