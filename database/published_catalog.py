"""Export the derived UPS facts without publishing extraction documents or paths."""

import argparse
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit

try:
    from .verified_evidence import _field as _conflict_field, has_unverified_bundle, _lifecycle_observation
except ImportError:
    from verified_evidence import _field as _conflict_field, has_unverified_bundle, _lifecycle_observation

SCHEMA = '''CREATE TABLE published_models(product_id INTEGER PRIMARY KEY,
manufacturer TEXT,model TEXT,sku TEXT,rating_w REAL,rating_va REAL,
outlet_count INTEGER,model_json TEXT NOT NULL);
CREATE TABLE catalog_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);'''
MODEL_COLUMNS = ('product_id', 'manufacturer', 'model', 'sku', 'rating_w',
                 'rating_va', 'outlet_count', 'model_json')
FORMAT = 'ups-curated-v2'
SUPPORTED_FORMATS = frozenset({'ups-curated-v1', FORMAT})


def _source(reference):
    if not isinstance(reference, dict):
        return None
    identity = json.dumps(reference, sort_keys=True, ensure_ascii=False)
    old_id = reference.get('sourceId')
    old_path = reference.get('path')
    already_projected = (isinstance(old_id, str) and re.fullmatch(r'[0-9a-f]{24}', old_id)
                         and (old_path == 'source:' + old_id or old_path is None and reference.get('url')))
    result = {'sourceId': old_id if already_projected else hashlib.sha256(identity.encode()).hexdigest()[:24]}
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
        if isinstance(reference.get('page'), int) and not isinstance(reference['page'], bool) and reference['page'] > 0:
            result['page'] = reference['page']
        result.pop('path')
    return result


def _public_text(value, limit=1000):
    if (not isinstance(value, str) or len(value) > limit
            or value.startswith(chr(92) * 2)
            or re.search(r'(?i)(?:[a-z]:[/\\]|file://|https?://)', value)):
        return None
    return value


def _project_outlet_groups(groups):
    return [{'type': _public_text(group.get('type'), 80), 'power': _public_text(group.get('power'), 80),
             'manufacturer_verified': group.get('manufacturer_verified') is True,
             'count': group.get('count'), 'source': _source(group.get('source'))}
            for group in groups or [] if isinstance(group, dict)]


def _project_battery_configurations(configurations):
    result = []
    for config in configurations or []:
        if not isinstance(config, dict):
            continue
        projected = {key: _public_text(config.get(key)) for key in ('id', 'label', 'conditions')}
        projected['manufacturer_verified'] = config.get('manufacturer_verified') is True
        projected['source'] = _source(config.get('source'))
        projected['points'] = [{key: point.get(key) for key in ('loadW', 'runtimeMinutes')}
                               for point in config.get('points', []) if isinstance(point, dict)]
        for key in ('includedPacks', 'extraPacks'):
            projected[key] = [{'sku': _public_text(pack.get('sku'), 300), 'quantity': pack.get('quantity')}
                              for pack in config.get(key, []) if isinstance(pack, dict)]
        result.append(projected)
    return result


def project_model(product, include_prices=False):
    keys = ('id', 'manufacturer_id', 'manufacturer', 'model', 'sku', 'product_type',
            'rating_w', 'rating_va', 'outlet_count', 'external_battery_supported',
            'external_battery_model', 'max_external_battery_packs', 'warnings')
    result = {key: product.get(key) for key in keys}
    result.update(description=None, non_ups_reason=None, prices=[], estimated_cost=None)
    # Structured verified facts are independent of total-outlet and legacy runtime
    # fields. Their source references must receive the same privacy filtering as
    # the original evidence; never copy raw source records into the projection.
    result['identity_verified'] = product.get('identity_verified') is True
    result['bundle_identity_unverified'] = has_unverified_bundle(product)
    result['manufacturer_sku'] = product.get('manufacturer_sku')
    result['product_category'] = product.get('product_category')
    result['identity_source'] = _source(product.get('identity_source'))
    lifecycle = product.get('lifecycle_observations')
    result['lifecycle_observations'] = [
        observation for value in lifecycle if (observation := _lifecycle_observation(
            value, product.get('manufacturer'), product.get('manufacturer_sku')
            if product.get('identity_verified') is True else None))
    ] if isinstance(lifecycle, list) and isinstance(product.get('manufacturer'), str) else []
    crosswalk = product.get('identity_crosswalk')
    if isinstance(crosswalk, dict) and re.fullmatch(r'[0-9a-f]{24}', str(crosswalk.get('sourceId') or '')):
        maker_source = _source({'url': crosswalk.get('manufacturerUrl')})
        if maker_source and maker_source.get('url'):
            result['identity_crosswalk'] = {'sourceId': crosswalk['sourceId'],
                                             'manufacturerUrl': maker_source['url']}
    for field in ('rating_w', 'rating_va'):
        result[field + '_verified'] = product.get(field + '_verified') is True
        result[field + '_source'] = _source(product.get(field + '_source'))
    if product.get('non_ups_reason') == 'manufacturer_verified_non_ups':
        result['non_ups_reason'] = 'manufacturer_verified_non_ups'
    originals = product.get('original_source_ratings')
    if isinstance(originals, dict):
        result['original_source_ratings'] = {
            field: value for field, value in originals.items()
            if field in {'rating_w', 'rating_va'} and isinstance(value, (int, float))
            and not isinstance(value, bool) and math.isfinite(value)
        }
    result['manufacturer_conflicts'] = []
    for conflict in product.get('manufacturer_conflicts') or []:
        if not isinstance(conflict, dict):
            continue
        clean = {'field': _conflict_field(conflict.get('field'))}
        for key in ('configurationId', 'outletType'):
            value = conflict.get(key)
            if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', value):
                clean[key] = value
        for key in ('valueA', 'valueB'):
            value = conflict.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                clean[key] = value
            elif (isinstance(value, str) and len(value) <= 300 and '\\' not in value
                  and not re.search(r'(?i)[a-z]:/|https?://', value)):
                clean[key] = value
        for key in ('sourceA', 'sourceB'):
            source = _source(conflict.get(key))
            if source:
                clean[key] = source
        if clean.get('field'):
            result['manufacturer_conflicts'].append(clean)
    result['outlet_groups'] = _project_outlet_groups(product.get('outlet_groups'))
    result['battery_configurations'] = _project_battery_configurations(product.get('battery_configurations'))
    for field in ('identity_source', 'rating_w_source', 'rating_va_source'):
        key = 'previous_' + field
        if isinstance(product.get(key), list):
            result[key] = [source for value in product[key] if (source := _source(value))]
    if isinstance(product.get('previous_outlet_groups'), list):
        result['previous_outlet_groups'] = _project_outlet_groups(product['previous_outlet_groups'])
    if isinstance(product.get('previous_battery_configurations'), list):
        result['previous_battery_configurations'] = _project_battery_configurations(product['previous_battery_configurations'])
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
