"""User-controlled, non-commercial exploration of source-backed UPS models."""

import math
from contextlib import closing
from pathlib import Path
import sqlite3

try:
    from .recommendation_engine import (RecommendationInputError, _build_inventory,
        _apply_manufacturer_curves, _read_curve_catalog, _interpolate_runtime)
    from .optimization_engine import _price_comparison_basis
except ImportError:
    from recommendation_engine import (RecommendationInputError, _build_inventory,
        _apply_manufacturer_curves, _read_curve_catalog, _interpolate_runtime)
    from optimization_engine import _price_comparison_basis


def _number(request, key, default, minimum, maximum=None, integer=False):
    value = request.get(key, default)
    if isinstance(value, bool):
        raise RecommendationInputError(f'{key} must be numeric')
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise RecommendationInputError(f'{key} must be numeric')
    if (not math.isfinite(number) or number < minimum or
            maximum is not None and number > maximum or integer and not number.is_integer()):
        raise RecommendationInputError(f'{key} is outside its supported range')
    return int(number) if integer else number


def _boolean(request, key, default):
    value = request.get(key, default)
    if not isinstance(value, bool):
        raise RecommendationInputError(f'{key} must be boolean')
    return value


def _check(required, available, reason=None):
    if required <= 0:
        return {'status': 'pass', 'required': required, 'available': available, 'shortfall': 0}
    if available is None:
        return {'status': 'unknown', 'required': required, 'available': None,
                'shortfall': None, 'reason': reason or 'missing_evidence'}
    shortfall = max(0, required - available)
    return {'status': 'pass' if shortfall <= 1e-9 else 'fail', 'required': required,
            'available': available, 'shortfall': round(shortfall, 6)}


def _dataset_metadata(connection):
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='catalog_meta'").fetchone():
        return None
    allowed = {'format', 'generated_at', 'model_count', 'original_model_count',
               'omitted_missing_capacity_count', 'includes_prices', 'includes_incomplete'}
    metadata = {}
    for row in connection.execute('SELECT key,value FROM catalog_meta'):
        key, value = row[0], row[1]
        if key not in allowed:
            continue
        if key in {'model_count', 'original_model_count', 'omitted_missing_capacity_count'}:
            try:
                number = int(value)
                metadata[key] = number if number >= 0 else None
            except (ValueError, TypeError, OverflowError):
                metadata[key] = None
        elif key in {'includes_prices', 'includes_incomplete'}:
            metadata[key] = {'true': True, 'false': False}.get(str(value).lower())
        else:
            metadata[key] = str(value)[:80]
    return metadata


def search_catalog_from_connection(connection, request, curve_catalog=None):
    """Return filtered UPS models; exploration never certifies an unknown fact."""
    if not isinstance(request, dict):
        raise RecommendationInputError('Request body must be a JSON object')
    mode = request.get('mode', 'strict')
    if mode not in {'strict', 'nearby'}:
        raise RecommendationInputError('mode must be strict or nearby')
    expansion = request.get('expansion', 'any')
    if expansion not in {'any', 'supported'}:
        raise RecommendationInputError('expansion must be any or supported')
    query = request.get('query', '')
    manufacturer = request.get('manufacturer', '')
    if not isinstance(query, str) or not isinstance(manufacturer, str):
        raise RecommendationInputError('query and manufacturer must be strings')
    required_w = _number(request, 'requiredWatts', 0, 0)
    required_va = _number(request, 'requiredVa', 0, 0)
    outlets = _number(request, 'requiredOutlets', 0, 0, integer=True)
    runtime = _number(request, 'runtimeRequirementMinutes', 0, 0)
    headroom = _number(request, 'headroomPercent', 20, 0, 50)
    if not all(math.isfinite(value * (1 + headroom / 100)) for value in (required_w, required_va)):
        raise RecommendationInputError('Required load and headroom exceed the supported numeric range')
    tolerance = _number(request, 'nearbyTolerancePercent', 20, 0, 50)
    require_runtime = _boolean(request, 'requireRuntime', True)
    require_outlets = _boolean(request, 'requireOutlets', True)
    include_unknown = _boolean(request, 'includeUnknown', False)
    page = _number(request, 'page', 1, 1, 1000000, integer=True)
    page_size = _number(request, 'pageSize', 25, 1, 25, integer=True)
    budget = None
    if request.get('maxPriceTHB') is not None:
        budget = _number(request, 'maxPriceTHB', None, 0)
        if budget <= 0:
            raise RecommendationInputError('maxPriceTHB must be greater than zero')
    if connection.row_factory is None:
        connection.row_factory = sqlite3.Row
    dataset = _dataset_metadata(connection)
    inventory = _build_inventory(connection)
    _apply_manufacturer_curves(inventory, _read_curve_catalog(curve_catalog))
    inventory = [p for p in inventory if not p.get('non_ups_reason')]
    manufacturers = sorted({p['manufacturer'] for p in inventory if p.get('manufacturer')})
    coverage = {'totalModels': len(inventory), 'capacityKnown': 0, 'outletsKnown': 0,
                'runtimeCurveKnown': 0, 'optimizerEligible': 0,
                'matches': 0, 'nearby': 0, 'unknown': 0,
                'budgetExcludedUnknownPrice': 0, 'budgetExcludedOverPrice': 0}
    rows = []
    for product in inventory:
        capacity_known = product['rating_w'] is not None and product['rating_va'] is not None
        can_optimize = capacity_known and product['outlet_count'] is not None and product['runtime_curve'] is not None
        coverage['capacityKnown'] += int(capacity_known)
        coverage['outletsKnown'] += int(product['outlet_count'] is not None)
        coverage['runtimeCurveKnown'] += int(product['runtime_curve'] is not None)
        coverage['optimizerEligible'] += int(can_optimize)
        if manufacturer and (product.get('manufacturer') or '').casefold() != manufacturer.casefold():
            continue
        if query.strip().casefold() not in ' '.join(str(product.get(k) or '') for k in ('manufacturer','model','sku','description')).casefold():
            continue
        if expansion == 'supported' and product['external_battery_supported'] is not True:
            continue
        price_candidate = {'price_offers': product.get('prices') or []}
        price_basis = _price_comparison_basis([price_candidate])
        price = ({'amount': float(price_candidate['_cost']), 'currency': 'THB',
                  'vatBasis': 'exclusive'} if price_basis[0] else None)
        if budget is not None:
            if price is None:
                coverage['budgetExcludedUnknownPrice'] += 1
                continue
            if price['amount'] > budget:
                coverage['budgetExcludedOverPrice'] += 1
                continue
        curve = product['runtime_curve']
        minutes = _interpolate_runtime(curve['points'], required_w) if curve and required_w > 0 else None
        runtime_reason = 'missing_runtime_curve'
        if curve:
            if required_w <= 0:
                runtime_reason = 'load_not_specified'
            elif required_w < min(point['loadW'] for point in curve['points']):
                runtime_reason = 'load_below_curve_range'
            elif required_w > max(point['loadW'] for point in curve['points']):
                runtime_reason = 'load_above_curve_range'
        checks = {
            'capacityWatts': _check(required_w * (1 + headroom / 100), product['rating_w']),
            'capacityVa': _check(required_va * (1 + headroom / 100), product['rating_va']),
            'outlets': _check(outlets, product['outlet_count']),
            'runtime': _check(runtime, minutes, runtime_reason),
        }
        checks['capacityWatts']['enabled'] = required_w > 0
        checks['capacityVa']['enabled'] = required_va > 0
        checks['outlets']['enabled'] = require_outlets and outlets > 0
        checks['runtime']['enabled'] = require_runtime and runtime > 0
        active = [v for v in checks.values() if v['enabled']]
        failures = [v for v in active if v['status'] == 'fail']
        unknowns = [v for v in active if v['status'] == 'unknown']
        deficits = [v['shortfall'] / v['required'] * 100 for v in failures]
        status = 'unknown' if unknowns else 'nearby' if failures else 'matches'
        coverage[status] += 1
        if mode == 'strict' and status != 'matches':
            continue
        if mode == 'nearby' and (unknowns and not include_unknown or any(d > tolerance + 1e-9 for d in deficits)):
            continue
        warnings = []
        if unknowns:
            warnings.append('Required technical evidence is unknown; this model is not a verified match.')
        if failures:
            warnings.append('One or more requirements fail; review the explicit shortfalls.')
        if minutes is None and runtime > 0:
            warnings.append('Runtime was not extrapolated beyond source curve points.')
        row = {
            'productId': product['id'], 'manufacturer': product['manufacturer'],
            'model': product['model'], 'sku': product['sku'],
            'ratings': {'watts': product['rating_w'], 'va': product['rating_va']},
            'outletCount': product['outlet_count'],
            'runtimeMinutes': None if minutes is None else round(minutes, 4),
            'runtimeLoadRangeWatts': None if not curve else {
                'minimum': min(p['loadW'] for p in curve['points']),
                'maximum': max(p['loadW'] for p in curve['points'])},
            'status': status, 'checks': checks, 'warnings': warnings,
            'hasSourceConflicts': any('conflict' in warning.casefold() or 'disagree' in warning.casefold()
                                      for warning in product.get('warnings', [])),
            'price': price, 'priceUnavailableReason': None if price else price_basis[1],
            'canOptimize': can_optimize,
            'expansion': {'supported': product['external_battery_supported'],
                          'maxBatteryPacks': product['max_external_battery_packs']},
        }
        capacity_fit = sum(abs(checks[key]['available'] - checks[key]['required']) / checks[key]['required']
                           for key in ('capacityWatts', 'capacityVa')
                           if checks[key]['enabled'] and checks[key]['available'] is not None)
        rows.append((len(unknowns), len(failures), sum(deficits), capacity_fit, row))
    rows.sort(key=lambda item: (item[0], item[1], item[2], item[3],
                               item[4]['manufacturer'] or '', item[4]['model'] or '', item[4]['productId']))
    return {'dataset': dataset,
            'models': [r[4] for r in rows[(page - 1) * page_size:page * page_size]],
            'total': len(rows), 'page': page, 'pageSize': page_size,
            'facets': {'manufacturers': manufacturers}, 'coverage': coverage,
            'requirements': {'requiredWatts': required_w, 'requiredVa': required_va,
                'requiredOutlets': outlets, 'runtimeRequirementMinutes': runtime,
                'headroomPercent': headroom, 'requireRuntime': require_runtime,
                'requireOutlets': require_outlets, 'includeUnknown': include_unknown,
                'nearbyTolerancePercent': tolerance, 'mode': mode, 'maxPriceTHB': budget}}


def search_catalog(database_path, request, curve_catalog=None):
    path = Path(database_path).resolve()
    if not path.is_file():
        raise FileNotFoundError('UPS database is unavailable')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as connection:
        return search_catalog_from_connection(connection, request, curve_catalog)
