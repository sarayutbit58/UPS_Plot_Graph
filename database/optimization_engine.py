"""Bounded CSP search for source-backed UPS fleet designs."""

import copy
from datetime import date
from decimal import Decimal
import itertools
import math
from pathlib import Path
import re
import sqlite3

try:
    from .allocation_engine import (
        AllocationInputError,
        _new_unit,
        _parse_request,
        _placement,
        _plan_outlets,
        _commit_outlets,
        _rollback_outlets,
        _technical_evidence,
    )
    from .recommendation_engine import (
        MIN_ENGINEERING_MARGIN,
        _apply_manufacturer_curves,
        _build_inventory,
        _interpolate_runtime,
        _read_curve_catalog,
        _verified_battery_outlet_count,
        _verified_battery_outlet_groups,
        _verified_runtime_curve,
        _verified_runtime_curves,
        _verified_rating,
        _verified_identity,
    )
except ImportError:  # Running this file or sibling API module directly.
    from allocation_engine import (
        AllocationInputError,
        _new_unit,
        _parse_request,
        _placement,
        _plan_outlets,
        _commit_outlets,
        _rollback_outlets,
        _technical_evidence,
    )
    from recommendation_engine import (
        MIN_ENGINEERING_MARGIN,
        _apply_manufacturer_curves,
        _build_inventory,
        _interpolate_runtime,
        _read_curve_catalog,
        _verified_battery_outlet_count,
        _verified_battery_outlet_groups,
        _verified_runtime_curve,
        _verified_runtime_curves,
        _verified_rating,
        _verified_identity,
    )


MAX_SEARCH_UNITS = 12
MAX_DISTINCT_MODELS = 3
MAX_FLEET_CANDIDATES = 2_000
MAX_SEARCH_WORK = 2_000_000
MAX_ASSIGNMENT_NODES_PER_FLEET = 1_000
MAX_ASSIGNMENT_NODES_TOTAL = 250_000
MAX_EXACT_ASSIGNMENT_INSTANCES = 200
TOP_ALTERNATIVES = 5
USER_CONFIRMED_PRICE_CURRENCY = 'THB'
VERIFIED_THAI_VAT_PERIODS = (
    (date(2025, 10, 1), date(2027, 9, 30), Decimal('7')),
)


class OptimizationInputError(AllocationInputError):
    """Optimization request fields cannot be used to design a UPS fleet."""


def _verified_thai_vat_rate(effective_from, effective_to):
    if not effective_from or not effective_to:
        return None
    try:
        starts_at = date.fromisoformat(str(effective_from))
        ends_at = date.fromisoformat(str(effective_to))
    except ValueError:
        return None
    if starts_at > ends_at:
        return None
    for starts_on, ends_on, rate in VERIFIED_THAI_VAT_PERIODS:
        if starts_on <= starts_at and ends_at <= ends_on:
            return (rate, {'startsOn': starts_on.isoformat(), 'endsOn': ends_on.isoformat()},
                    {'startsOn': starts_at.isoformat(), 'endsOn': ends_at.isoformat()})
    return None


def _price_comparison_basis(candidates):
    """Validate prices and store normalized comparison costs on candidates."""
    def unavailable(code, reason, currency=None, currency_source=None, missing_currency_count=0):
        return (False, code, reason, currency, currency_source, missing_currency_count,
                None, 0, None, [], [])

    if any((candidate.get('runtime_curve') or {}).get('extraPacks') for candidate in candidates):
        return unavailable('battery_pack_price_unknown',
                           'ยังไม่มีราคาแบตเตอรี่เสริมครบ จึงเทียบต้นทุนรวมของชุดนี้ไม่ได้')
    price_groups = [candidate.get('price_offers') or [] for candidate in candidates]
    if not price_groups or any(not group for group in price_groups):
        return unavailable('missing_price', 'ราคาของ UPS ที่เข้าเกณฑ์ยังไม่ครบทุกโมเดล')
    prices = [price for group in price_groups for price in group]
    currencies = {str(price.get('currency') or '').strip().upper() for price in prices
                  if str(price.get('currency') or '').strip()}
    missing_currency_count = sum(not str(price.get('currency') or '').strip() for price in prices)
    currency_source = 'user-confirmed' if missing_currency_count else 'catalog'
    if len(currencies) > 1:
        return unavailable('mixed_currency', 'สกุลเงินของราคาแต่ละโมเดลไม่ตรงกัน',
                           None, 'conflicting-source', missing_currency_count)
    if currencies and currencies != {USER_CONFIRMED_PRICE_CURRENCY}:
        currency = next(iter(currencies))
        return unavailable('non_thb_currency', 'พบราคาในสกุลเงินอื่นที่ต้องแปลงก่อนเทียบ',
                           currency, 'catalog', missing_currency_count)
    currency = USER_CONFIRMED_PRICE_CURRENCY

    normalized_groups = []
    inclusive_count = 0
    rates_used = set()
    vat_periods_used = set()
    offer_periods_used = set()
    for candidate, group in zip(candidates, price_groups):
        normalized_offers = []
        for offer in group:
            treatment = str(offer.get('vatTreatment') or '').strip().casefold()
            if treatment not in {'exclusive', 'inclusive'}:
                return unavailable('missing_vat', 'มีราคาไม่ทราบ VAT จึงเปรียบเทียบราคาไม่รวม VAT ไม่ได้',
                                   currency, currency_source, missing_currency_count)
            try:
                amount = offer.get('decimal')
                amount = amount if isinstance(amount, Decimal) else Decimal(str(amount or offer.get('amount')))
            except Exception:
                return unavailable('missing_price', 'ราคา UPS บางรายการไม่ใช่จำนวนเงินที่ใช้เปรียบเทียบได้',
                                   currency, currency_source, missing_currency_count)
            if not amount.is_finite() or amount <= 0:
                return unavailable('missing_price', 'ราคา UPS บางรายการไม่ใช่จำนวนเงินที่ใช้เปรียบเทียบได้',
                                   currency, currency_source, missing_currency_count)
            if treatment == 'exclusive':
                net_amount = amount
            else:
                rate_details = _verified_thai_vat_rate(
                    offer.get('priceEffectiveFrom'), offer.get('priceEffectiveTo'))
                if rate_details is None:
                    return unavailable(
                        'unverified_vat_rate',
                        'ราคา inclusive ไม่มีช่วงวันที่มีผลซึ่งยืนยันอัตรา VAT ได้ จึงแปลงเป็นราคาไม่รวม VAT ไม่ได้',
                        currency, currency_source, missing_currency_count,
                    )
                rate, vat_period, offer_period = rate_details
                net_amount = amount * Decimal('100') / (Decimal('100') + rate)
                inclusive_count += 1
                rates_used.add(rate)
                vat_periods_used.add((vat_period['startsOn'], vat_period['endsOn']))
                offer_periods_used.add((offer_period['startsOn'], offer_period['endsOn']))
            normalized_offers.append((net_amount, offer))
        normalized_groups.append((candidate, min(
            normalized_offers, key=lambda item: (item[0], item[1].get('kind') or '')
        )))

    for candidate, (net_amount, offer) in normalized_groups:
        candidate['_cost'] = net_amount
        candidate['_comparison_cost_source_date'] = offer.get('sourceDate')

    rate_percent = float(next(iter(rates_used))) if len(rates_used) == 1 else None
    rate_periods = [
        {'startsOn': starts_on, 'endsOn': ends_on}
        for starts_on, ends_on in sorted(vat_periods_used)
    ]
    offer_periods = [
        {'startsOn': starts_on, 'endsOn': ends_on}
        for starts_on, ends_on in sorted(offer_periods_used)
    ]
    return (True, None, None, currency, currency_source, missing_currency_count,
            'exclusive', inclusive_count, rate_percent, rate_periods, offer_periods)


def _candidate_models(connection, curve_catalog):
    if connection.row_factory is None:
        connection.row_factory = sqlite3.Row
    inventory = _build_inventory(connection)
    _apply_manufacturer_curves(inventory, _read_curve_catalog(curve_catalog))
    candidates = []
    for product in inventory:
        if product.get('non_ups_reason') or not _verified_identity(product):
            continue
        backed_outlets = _verified_battery_outlet_count(product)
        rating_w = _verified_rating(product, 'rating_w')
        rating_va = _verified_rating(product, 'rating_va')
        if rating_w is None or rating_va is None or not backed_outlets:
            continue
        curves = _verified_runtime_curves(product)
        price = product.get('estimated_cost')
        cost = None
        if price:
            try:
                parsed_cost = Decimal(str(price.get('amount')))
            except Exception:
                parsed_cost = None
            if parsed_cost is not None and parsed_cost.is_finite() and parsed_cost > 0:
                cost = parsed_cost
        for curve in curves:
            candidates.append({
            'product_id': product['id'],
            'manufacturer': product.get('manufacturer'),
            'model': product.get('model'),
            'sku': product.get('sku'),
            'identity_verified': True,
            'rating_w': rating_w,
            'rating_va': rating_va,
            'rating_w_source': product.get('rating_w_source'),
            'rating_va_source': product.get('rating_va_source'),
            'identity_source': product.get('identity_source'),
            'outlet_count': backed_outlets,
            'outlet_groups': _verified_battery_outlet_groups(product),
            'runtime_curve': curve,
            'battery_config_id': curve['configurationId'],
            'external_battery_supported': product.get('external_battery_supported'),
            'external_battery_model': product.get('external_battery_model'),
            'max_external_battery_packs': product.get('max_external_battery_packs'),
            'estimated_cost': price,
            'price_offers': list(product.get('prices', [])),
            '_cost': cost,
            'evidence': list(product.get('evidence', [])),
            'warnings': [warning for warning in product.get('warnings', [])
                         if 'price' not in warning.casefold()],
            'source_warnings': [warning for warning in product.get('warnings', [])
                                if 'price' not in warning.casefold()],
            'manufacturer_outlet_source': product.get('manufacturer_outlet_source'),
            })
    candidates.sort(key=lambda item: (item['product_id'], item['battery_config_id']))
    return candidates


def _parse_optimization_request(request, candidates):
    if not isinstance(request, dict):
        raise OptimizationInputError('Request body must be a JSON object')
    validation_request = dict(request)
    zero_load_indexes = set()
    input_devices = request.get('devices')
    if isinstance(input_devices, list):
        validated_devices = []
        for index, device in enumerate(input_devices):
            try:
                watt = Decimal(str(device.get('watt', device.get('watts'))).strip()) if isinstance(device, dict) else None
                va = Decimal(str(device.get('va')).strip()) if isinstance(device, dict) else None
            except Exception:
                watt = va = None
            if (watt is not None and va is not None and watt.is_finite() and va.is_finite()
                    and watt == 0 and va == 0):
                validation_row = dict(device)
                validation_row['watt'] = 1
                validation_row['va'] = 1
                validated_devices.append(validation_row)
                zero_load_indexes.add(index)
            else:
                validated_devices.append(device)
        validation_request['devices'] = validated_devices
    # Reuse the allocation input validator without restricting the search pool.
    validation_request['selectedUps'] = {
        'mode': 'auto',
        'models': [{'productId': candidates[0]['product_id'] if candidates else 1}],
    }
    try:
        parsed = _parse_request(validation_request)
    except AllocationInputError as error:
        raise OptimizationInputError(str(error)) from error
    for index in zero_load_indexes:
        device = parsed['devices'][index]
        parsed['required_watts'] -= device['watt_each'] * device['quantity']
        parsed['required_va'] -= device['va_each'] * device['quantity']
        device['watt_each'] = 0.0
        device['va_each'] = 0.0
    if parsed['required_watts'] <= 0 or parsed['required_va'] <= 0:
        raise OptimizationInputError('At least one device must have a positive watt and VA load')
    return parsed


def _minimum_units(parsed, candidates):
    maximum_w = max(candidate['rating_w'] / (1 + float(MIN_ENGINEERING_MARGIN)) for candidate in candidates)
    maximum_va = max(candidate['rating_va'] / (1 + float(MIN_ENGINEERING_MARGIN)) for candidate in candidates)
    maximum_outlets = max(candidate['outlet_count'] for candidate in candidates)
    return max(
        1,
        math.ceil(parsed['required_watts'] / maximum_w),
        math.ceil(parsed['required_va'] / maximum_va),
        math.ceil(parsed['required_outlets'] / maximum_outlets),
    )


def _fleet_count_vectors(unit_count, candidate_count):
    """Generate integer model quantities for one to three distinct models."""
    for index in range(candidate_count):
        yield ((index, unit_count),)
    if unit_count < 2:
        return
    for first, second in itertools.combinations(range(candidate_count), 2):
        for first_count in range(1, unit_count):
            yield ((first, first_count), (second, unit_count - first_count))
    if unit_count < 3:
        return
    for first, second, third in itertools.combinations(range(candidate_count), 3):
        for first_count in range(1, unit_count - 1):
            for second_count in range(1, unit_count - first_count):
                third_count = unit_count - first_count - second_count
                if third_count > 0:
                    yield ((first, first_count), (second, second_count), (third, third_count))


def _fleet_can_cover(parsed, fleet, candidates):
    required_w = parsed['required_watts']
    required_va = parsed['required_va']
    required_outlets = parsed['required_outlets']
    safe_w = sum(candidates[index]['rating_w'] / (1 + float(MIN_ENGINEERING_MARGIN)) * quantity
                 for index, quantity in fleet)
    safe_va = sum(candidates[index]['rating_va'] / (1 + float(MIN_ENGINEERING_MARGIN)) * quantity
                  for index, quantity in fleet)
    outlets = sum(candidates[index]['outlet_count'] * quantity for index, quantity in fleet)
    if safe_w + 1e-9 < required_w or safe_va + 1e-9 < required_va or outlets < required_outlets:
        return False
    models = [candidates[index] for index, _ in fleet]
    for device in parsed['devices']:
        if device['watt_each'] == 0 and device['va_each'] == 0:
            continue
        if not any(not _optimizer_placement(_new_unit(model, 1, False), device, parsed['runtime_required'])[0]
                   for model in models):
            return False
    return True


def _optimizer_placement(unit, device, runtime_required):
    if device['watt_each'] != 0 or device['va_each'] != 0:
        reasons, runtime, utilization = _placement(unit, device, runtime_required)
        curve = unit.get('runtime_curve')
        watts = unit['used_w'] + device['watt_each']
        # Partial assignments may later accumulate into the supported interval.
        # No runtime value is invented; completed fleets are checked separately.
        if curve and watts < min(point['loadW'] for point in curve['points']):
            reasons = [reason for reason in reasons if reason != 'runtime_unknown']
            if not reasons:
                safe_w = unit['rating_w'] / (1 + float(MIN_ENGINEERING_MARGIN))
                safe_va = unit['rating_va'] / (1 + float(MIN_ENGINEERING_MARGIN))
                utilization = max(watts / safe_w,
                    (unit['used_va'] + device['va_each']) / safe_va,
                    (len(unit['outlets']) + device['plug_count']) / unit['outlet_count'])
        return reasons, runtime, utilization
    outlet_number = len(unit['outlets']) + device['plug_count']
    _, outlet_reason = _plan_outlets(unit, device)
    reasons = [outlet_reason] if outlet_reason else []
    if reasons:
        return reasons, None, None
    if unit['used_w'] > 0 and unit['runtime_curve']:
        runtime = _interpolate_runtime(unit['runtime_curve']['points'], unit['used_w'])
        if runtime is None:
            return ['runtime_unknown'], None, None
        if runtime + 1e-9 < runtime_required:
            return ['runtime_insufficient'], runtime, None
    else:
        runtime = None
    safe_w = unit['rating_w'] / (1 + float(MIN_ENGINEERING_MARGIN))
    safe_va = unit['rating_va'] / (1 + float(MIN_ENGINEERING_MARGIN))
    utilization = max(unit['used_w'] / safe_w, unit['used_va'] / safe_va,
                      outlet_number / unit['outlet_count'])
    return [], runtime, utilization


def _new_fleet_units(fleet, candidates):
    units = []
    for index, quantity in fleet:
        for _ in range(quantity):
            units.append(_new_unit(candidates[index], len(units) + 1, False))
    return units


def _outlet_state(unit):
    """Connector demand, not just occupied socket numbers, affects future fits."""
    demands = {}
    for groups in unit['plug_demands']:
        for group in groups:
            connector = group['connector'] or ''
            demands[connector] = demands.get(connector, 0) + group['count']
    return tuple(sorted(demands.items()))


def _greedy_assignment(parsed, fleet, candidates):
    orderings = (
        lambda row: (row['watt_each'] == 0 and row['va_each'] == 0,
                     -row['priority'], -max(row['watt_each'], row['va_each']),
                     -row['watt_each'], row['device_index']),
        lambda row: (row['watt_each'] == 0 and row['va_each'] == 0,
                     -row['priority'], -row['watt_each'], -row['va_each'], row['device_index']),
        lambda row: (row['watt_each'] == 0 and row['va_each'] == 0,
                     -row['priority'], -row['va_each'], -row['watt_each'], row['device_index']),
    )
    for order in orderings:
        trial_units = _new_fleet_units(fleet, candidates)
        success = True
        for device in sorted(parsed['devices'], key=order):
            for _ in range(device['quantity']):
                viable = []
                for unit in trial_units:
                    reasons, runtime, utilization = _optimizer_placement(unit, device, parsed['runtime_required'])
                    if not reasons:
                        viable.append((utilization, unit['sequence'], unit, runtime))
                if not viable:
                    success = False
                    break
                _, _, unit, _ = min(viable, key=lambda item: (-item[0], item[1]))
                unit['used_w'] += device['watt_each']
                unit['used_va'] += device['va_each']
                _commit_outlets(unit, device)
            if not success:
                break
        if success and all(
                (runtime := _interpolate_runtime(unit['runtime_curve']['points'], unit['used_w'])) is not None
                and runtime + 1e-9 >= parsed['runtime_required']
                for unit in trial_units if unit['used_w'] > 0):
            return trial_units
    return None


def _exact_assignment(parsed, fleet, candidates, node_limit, initial_units=None):
    """Enumerate symmetric placements to find feasible and higher-runtime assignments."""
    instances = []
    for device in parsed['devices']:
        instances.extend([device] * device['quantity'])
    instances.sort(key=lambda row: (
        row['watt_each'] == 0 and row['va_each'] == 0,
        -row['priority'], -max(row['watt_each'], row['va_each']),
        -row['watt_each'], -row['va_each'], row['device_index'],
    ))
    units = _new_fleet_units(fleet, candidates)
    best_units = copy.deepcopy(initial_units) if initial_units else None
    best_runtime = None
    best_used_unit_count = None
    if best_units:
        initial_runtimes = [
            _interpolate_runtime(unit['runtime_curve']['points'], unit['used_w'])
            for unit in best_units if unit['used_w'] > 0
        ]
        if initial_runtimes and all(value is not None for value in initial_runtimes):
            best_runtime = min(initial_runtimes)
            best_used_unit_count = sum(bool(unit['outlets']) for unit in best_units)

    visited_states = set()
    nodes = 0
    complete = True

    def recurse(device_index):
        nonlocal best_units, best_runtime, best_used_unit_count, nodes, complete
        if nodes >= node_limit:
            complete = False
            return
        nodes += 1
        state = (device_index, tuple(sorted(
            (unit['product_id'], str(unit.get('battery_config_id') or ''),
             unit['used_w'].hex(), unit['used_va'].hex(), _outlet_state(unit))
            for unit in units
        )))
        if state in visited_states:
            return
        visited_states.add(state)

        active_runtimes = [
            _interpolate_runtime(unit['runtime_curve']['points'], unit['used_w'])
            for unit in units if unit['used_w'] > 0
        ]
        known_runtimes = [runtime for runtime in active_runtimes if runtime is not None]
        if (best_runtime is not None and known_runtimes
                and min(known_runtimes) + 1e-9 < best_runtime):
            return

        if device_index >= len(instances):
            active_units = [unit for unit in units if unit['outlets']]
            if not active_units:
                return
            loaded_units = [unit for unit in active_units if unit['used_w'] > 0]
            runtimes = [_interpolate_runtime(unit['runtime_curve']['points'], unit['used_w'])
                        for unit in loaded_units]
            if any(value is None or value + 1e-9 < parsed['runtime_required'] for value in runtimes):
                return
            current_runtime = min(runtimes)
            current_count = len(active_units)
            if (best_runtime is None or current_runtime > best_runtime + 1e-9
                    or (abs(current_runtime - best_runtime) <= 1e-9
                        and current_count < best_used_unit_count)):
                best_units = copy.deepcopy(active_units)
                best_runtime = current_runtime
                best_used_unit_count = current_count
            return

        device = instances[device_index]
        viable = []
        seen_unit_states = set()
        for unit in units:
            reasons, _, utilization = _optimizer_placement(unit, device, parsed['runtime_required'])
            if reasons:
                continue
            equivalent_state = (unit['product_id'], str(unit.get('battery_config_id') or ''),
                                unit['used_w'].hex(), unit['used_va'].hex(),
                                _outlet_state(unit))
            if equivalent_state in seen_unit_states:
                continue
            seen_unit_states.add(equivalent_state)
            viable.append((utilization, unit['sequence'], unit))
        viable.sort(key=lambda item: (item[0], item[1]))
        for _, _, unit in viable:
            unit['used_w'] += device['watt_each']
            unit['used_va'] += device['va_each']
            _commit_outlets(unit, device)
            recurse(device_index + 1)
            unit['used_w'] -= device['watt_each']
            unit['used_va'] -= device['va_each']
            _rollback_outlets(unit)
            if not complete and nodes >= node_limit:
                break

    if node_limit > 0:
        recurse(0)
    else:
        complete = False
    return best_units, complete, nodes


def _assignment_summaries(parsed, fleet, candidates, assigned_units):
    if assigned_units is None:
        return None

    assigned_units = [unit for unit in assigned_units if unit['outlets']]
    summaries = []
    model_by_id = {(candidate['product_id'], candidate['battery_config_id']): candidate
                   for candidate in candidates}
    for unit in assigned_units:
        zero_load = unit['used_w'] == 0 and unit['used_va'] == 0
        runtime = None if zero_load else _interpolate_runtime(unit['runtime_curve']['points'], unit['used_w'])
        if not zero_load and (runtime is None or runtime + 1e-9 < parsed['runtime_required']):
            return None
        model = model_by_id[(unit['product_id'], unit['battery_config_id'])]
        summaries.append({
            'unitId': unit['ups_unit_id'], 'productId': unit['product_id'],
            'batteryConfigId': unit['battery_config_id'],
            'manufacturer': unit['manufacturer'], 'model': unit['model'], 'sku': unit['sku'],
            'ratings': {'watts': unit['rating_w'], 'va': unit['rating_va']},
            'usedLoad': {'watts': round(unit['used_w'], 2), 'va': round(unit['used_va'], 2)},
            'outlets': {'used': len(unit['outlets']), 'available': unit['outlet_count']},
            'estimatedRuntimeMinutes': None if runtime is None else round(runtime, 2),
            'runtimeStatus': 'not_applicable_zero_load' if zero_load else 'meets_requirement',
            'engineeringHeadroomPercent': {
                'watts': None if zero_load else round((unit['rating_w'] - unit['used_w']) / unit['used_w'] * 100, 2),
                'va': None if zero_load else round((unit['rating_va'] - unit['used_va']) / unit['used_va'] * 100, 2),
            },
            'technicalEvidence': _technical_evidence(model),
            'sourceWarnings': list(model['warnings']),
        })
    return summaries


def _assign_devices(parsed, fleet, candidates, node_limit):
    greedy_units = _greedy_assignment(parsed, fleet, candidates)
    if parsed['device_count'] > MAX_EXACT_ASSIGNMENT_INSTANCES:
        return _assignment_summaries(parsed, fleet, candidates, greedy_units), False, 0
    assigned_units, complete, nodes = _exact_assignment(
        parsed, fleet, candidates, node_limit, initial_units=greedy_units)
    summaries = _assignment_summaries(parsed, fleet, candidates, assigned_units)
    return summaries, complete, nodes


def _solution(parsed, fleet, candidates, units, price_comparable, price_currency, price_currency_source):
    counts = {}
    for unit in units:
        key = (unit['productId'], unit['batteryConfigId'])
        counts[key] = counts.get(key, 0) + 1
    models = [candidate for candidate in candidates
              if (candidate['product_id'], candidate['battery_config_id']) in counts]
    loaded_units = [unit for unit in units if unit['estimatedRuntimeMinutes'] is not None]
    min_runtime = min(unit['estimatedRuntimeMinutes'] for unit in loaded_units)
    average_runtime = sum(unit['estimatedRuntimeMinutes'] for unit in loaded_units) / len(loaded_units)
    expansion_units = 0
    expansion_packs = 0
    unknown_expansion_units = 0
    for model in models:
        quantity = counts[(model['product_id'], model['battery_config_id'])]
        if model['external_battery_supported'] is True and model['max_external_battery_packs'] is not None:
            expansion_units += quantity
            expansion_packs += quantity * model['max_external_battery_packs']
        elif model['external_battery_supported'] is None or model['external_battery_supported']:
            unknown_expansion_units += quantity
    total_cost = (sum((model['_cost'] * counts[(model['product_id'], model['battery_config_id'])]
                       for model in models), Decimal('0'))
                  if price_comparable else None)
    signature = ','.join(f"{model['product_id']}:{model['battery_config_id']}x"
                         f"{counts[(model['product_id'], model['battery_config_id'])]}" for model in models)
    model_rows = []
    for model in models:
        model_rows.append({
            'productId': model['product_id'], 'manufacturer': model['manufacturer'],
            'model': model['model'], 'sku': model['sku'],
            'batteryConfigId': model['battery_config_id'],
            'batteryConfiguration': model['runtime_curve']['batteryConfiguration'],
            'includedPacks': model['runtime_curve']['includedPacks'],
            'extraPacks': model['runtime_curve']['extraPacks'],
            'quantity': counts[(model['product_id'], model['battery_config_id'])],
            'ratings': {'watts': model['rating_w'], 'va': model['rating_va']},
            'outletsPerUnit': model['outlet_count'],
            'externalBattery': {
                'supported': model['external_battery_supported'],
                'model': model['external_battery_model'],
                'maxPacksPerUnit': model['max_external_battery_packs'],
            },
        })
    headroom_values = [
        value
        for unit in units
        for value in unit['engineeringHeadroomPercent'].values()
        if value is not None
    ]
    headroom = min(headroom_values) if headroom_values else None
    zero_load_unit_count = sum(unit['runtimeStatus'] == 'not_applicable_zero_load' for unit in units)
    connectors = [plug['connector'] for device in parsed['devices'] for plug in device['plugs']]
    connector_status = ('not_checked' if all(connector is None for connector in connectors)
                        else 'partial' if any(connector is None for connector in connectors)
                        else 'verified')
    justifications = [
        f"ผ่านโหลดรวม {parsed['required_watts']:.2f} W / {parsed['required_va']:.2f} VA ด้วย UPS {len(units)} เครื่อง",
        f"UPS ที่รับโหลดจริงคง headroom อย่างน้อย {headroom:.1f}% เทียบกับโหลดที่จัดสรร",
        f"Runtime ต่ำสุด {min_runtime:.2f} นาทีของ UPS ที่รับโหลดจริง เทียบเป้าหมาย {parsed['runtime_required']:.2f} นาที",
        f"รองรับ outlet รวม {sum(unit['outlets']['used'] for unit in units)} รูจากจำนวนที่มีแหล่งข้อมูล",
    ]
    if zero_load_unit_count:
        justifications.append(f"UPS {zero_load_unit_count} เครื่องรับเฉพาะ outlet โหลด 0 W; Runtime ไม่เกี่ยวข้องกับโหลดศูนย์")
    if connector_status != 'verified':
        justifications.append('ยังไม่ได้ตรวจชนิดหัวปลั๊กทุกช่อง กรุณาตรวจความเข้ากันได้ก่อนใช้งาน')
    if expansion_packs:
        justifications.append(f"พบความจุแบตเตอรี่ขยายที่ยืนยันได้รวมสูงสุด {expansion_packs} packs")
    if unknown_expansion_units:
        justifications.append(f"ข้อมูล expansion ยังไม่ครบสำหรับ UPS {unknown_expansion_units} เครื่อง จึงไม่นับในคะแนนขยาย")
    if not price_comparable:
        justifications.append('ไม่ใช้ราคาเป็นตัวจัดอันดับ เพราะข้อมูลราคา สกุลเงิน หรือ VAT ยังเทียบกันไม่ครบ')
    else:
        if price_currency_source == 'user-confirmed':
            justifications.append('ใช้ THB ตามการยืนยันของผู้ใช้กับ price rows ที่เว้น currency')
    if price_comparable and any(not model.get('_comparison_cost_source_date') for model in models):
        justifications.append('บางราคาไม่มีวันที่ต้นทาง จึงควรตรวจราคาและวันมีผลก่อนสั่งซื้อ')
    return {
        'id': f'fleet-{signature}', 'signature': signature,
        'unitCount': len(units),
        'models': model_rows,
        'units': units,
        'load': {'watts': parsed['required_watts'], 'va': parsed['required_va'],
                 'deviceInstances': parsed['device_count']},
        'runtimeRequirementMinutes': parsed['runtime_required'],
        'minimumEstimatedRuntimeMinutes': round(min_runtime, 2),
        'averageEstimatedRuntimeMinutes': round(average_runtime, 2),
        'runtimeReserveMinutes': round(min_runtime - parsed['runtime_required'], 2),
        'expansion': {'verifiedUpsUnits': expansion_units, 'maxBatteryPacks': expansion_packs,
                      'unknownUpsUnits': unknown_expansion_units},
        'totalCost': None if total_cost is None else float(total_cost),
        'totalCostCurrency': price_currency if price_comparable else None,
        'validation': {'isValid': True, 'capacityValid': True, 'outletValid': True,
                       'runtimeValid': True, 'connectorStatus': connector_status,
                       'runtimeNotApplicableUnits': zero_load_unit_count,
                       'minimumEngineeringMarginPercent': 20},
        'engineeringJustification': justifications,
    }


def _rank_key(solution, scenario, price_comparable):
    cost = solution['totalCost'] if price_comparable else 0
    if scenario == 'A':
        return (cost, solution['unitCount'], -solution['runtimeReserveMinutes'], solution['signature'])
    if scenario == 'B':
        return (-solution['minimumEstimatedRuntimeMinutes'], solution['unitCount'],
                -solution['averageEstimatedRuntimeMinutes'], solution['signature'])
    if scenario == 'C':
        return (-solution['expansion']['maxBatteryPacks'], solution['unitCount'],
                -solution['runtimeReserveMinutes'], solution['signature'])
    if scenario == 'D':
        return (solution['unitCount'], -solution['runtimeReserveMinutes'], solution['signature'])
    return (solution['unitCount'], cost, -solution['runtimeReserveMinutes'], solution['signature'])


def optimize_from_connection(connection, request, curve_catalog=None):
    """Find source-backed fleet designs and rank them under five planning scenarios."""
    if not isinstance(request, dict):
        raise OptimizationInputError('Request body must be a JSON object')
    selected_ids = request.get('selectedProductIds')
    selected_configs = request.get('selectedConfigurations')
    if selected_ids is not None and selected_configs is not None:
        raise OptimizationInputError('Specify selectedProductIds or selectedConfigurations, not both')
    if selected_ids is not None:
        if (not isinstance(selected_ids, list) or not 1 <= len(selected_ids) <= 100
                or any(not isinstance(value, int) or isinstance(value, bool) or value <= 0
                       for value in selected_ids)
                or len(set(selected_ids)) != len(selected_ids)):
            raise OptimizationInputError('selectedProductIds must contain 1 to 100 unique positive integers')
    requested = None
    if selected_ids is not None:
        requested = [(product_id, None) for product_id in selected_ids]
    elif selected_configs is not None:
        if not isinstance(selected_configs, list) or not 1 <= len(selected_configs) <= 100:
            raise OptimizationInputError('selectedConfigurations must contain 1 to 100 selections')
        requested = []
        for selection in selected_configs:
            if not isinstance(selection, dict):
                raise OptimizationInputError('Each selected configuration must be an object')
            product_id, config_id = selection.get('productId'), selection.get('batteryConfigId')
            if (not isinstance(product_id, int) or isinstance(product_id, bool) or product_id <= 0
                    or config_id is not None and (not isinstance(config_id, str)
                        or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}', config_id))):
                raise OptimizationInputError('A selected product or battery configuration is invalid')
            requested.append((product_id, config_id))
        if len(set(requested)) != len(requested):
            raise OptimizationInputError('Selected product and battery configuration pairs must be unique')
        for product_id, config_id in requested:
            if config_id is None and sum(other_id == product_id for other_id, _ in requested) > 1:
                raise OptimizationInputError('Select explicit battery configurations when a product is repeated')
    candidates = _candidate_models(connection, curve_catalog)
    excluded_selected = []
    if requested is not None:
        inventory = _build_inventory(connection)
        _apply_manufacturer_curves(inventory, _read_curve_catalog(curve_catalog))
        by_id = {product['id']: product for product in inventory}
        allowed = set()
        for product_id, config_id in requested:
            matches = [candidate for candidate in candidates if candidate['product_id'] == product_id
                       and (config_id is None or candidate['battery_config_id'] == config_id)]
            if len(matches) == 1:
                allowed.add((product_id, matches[0]['battery_config_id']))
                continue
            product = by_id.get(product_id)
            if len(matches) > 1 and config_id is None:
                reasons = ['battery_configuration_required']
            elif product is None or product.get('non_ups_reason'):
                reasons = ['not_an_available_ups']
            else:
                reasons = [reason for key, reason in (
                    ('rating_w', 'watt_capacity_unknown'), ('rating_va', 'va_capacity_unknown'))
                    if _verified_rating(product, key) is None]
                if _verified_runtime_curve(product, config_id) is None:
                    reasons.append('runtime_unknown')
                if not _verified_identity(product):
                    reasons.insert(0, 'identity_unverified')
                backed_outlets = _verified_battery_outlet_count(product)
                if backed_outlets is None:
                    reasons.append('outlet_unknown')
                elif backed_outlets == 0:
                    reasons.append('outlet_capacity')
            diagnostic = {'productId': product_id, 'reasons': reasons}
            if selected_configs is not None:
                diagnostic['batteryConfigId'] = config_id
            excluded_selected.append(diagnostic)
        candidates = [candidate for candidate in candidates
                      if (candidate['product_id'], candidate['battery_config_id']) in allowed]
    parsed = _parse_optimization_request(request, candidates)
    (price_comparable, price_reason_code, price_reason, price_currency,
     price_currency_source, missing_currency_offers, comparable_vat,
     inclusive_offers_normalized, vat_rate_percent, vat_rate_periods,
     price_effective_periods) = _price_comparison_basis(candidates)
    missing_price_dates = sum(
        1 for candidate in candidates
        if not (candidate.get('_comparison_cost_source_date') if price_comparable
                else (candidate.get('estimated_cost') or {}).get('sourceDate'))
    )
    candidate_count = len(candidates)
    minimum_units = _minimum_units(parsed, candidates) if candidates else 1
    max_units_per_design = min(MAX_SEARCH_UNITS, parsed['device_count'])
    work_budget = max(20, min(MAX_FLEET_CANDIDATES,
                              MAX_SEARCH_WORK // max(1, parsed['device_count'] * MAX_SEARCH_UNITS)))
    valid_by_signature = {}
    fleet_evaluations = 0
    fleet_candidates_visited = 0
    assignment_nodes = 0
    assignment_search_complete = True
    pruned_fleets = 0
    reached_limit = False

    if candidates and minimum_units <= max_units_per_design:
        for unit_count in range(minimum_units, max_units_per_design + 1):
            for fleet in _fleet_count_vectors(unit_count, candidate_count):
                if fleet_candidates_visited >= work_budget:
                    reached_limit = True
                    break
                fleet_candidates_visited += 1
                if not _fleet_can_cover(parsed, fleet, candidates):
                    pruned_fleets += 1
                    continue
                remaining_nodes = MAX_ASSIGNMENT_NODES_TOTAL - assignment_nodes
                if remaining_nodes <= 0:
                    reached_limit = True
                    assignment_search_complete = False
                    break
                fleet_evaluations += 1
                units, placement_complete, placement_nodes = _assign_devices(
                    parsed, fleet, candidates,
                    min(MAX_ASSIGNMENT_NODES_PER_FLEET, remaining_nodes),
                )
                assignment_nodes += placement_nodes
                assignment_search_complete = assignment_search_complete and placement_complete
                if units is None:
                    continue
                solution = _solution(parsed, fleet, candidates, units, price_comparable,
                                     price_currency, price_currency_source)
                previous = valid_by_signature.get(solution['signature'])
                if previous is None or solution['minimumEstimatedRuntimeMinutes'] > previous['minimumEstimatedRuntimeMinutes']:
                    valid_by_signature[solution['signature']] = solution
            if reached_limit:
                break

    solutions = list(valid_by_signature.values())
    best_by_scenario = {}
    scenario_specs = [
        ('A', 'Lowest Cost', 'เปรียบเทียบราคาบนฐานไม่รวม VAT เมื่อสกุลเงินและ VAT ตรวจสอบได้'),
        ('B', 'Best Runtime', 'Runtime ต่ำสุดของ UPS ที่รับโหลดจริงสูงที่สุด; ใช้จำนวน UPS น้อยกว่าเมื่อเสมอกัน'),
        ('C', 'Maximum Expansion', 'จำนวน external battery packs ที่ยืนยันได้สูงที่สุด'),
        ('D', 'Minimum UPS Count', 'ใช้จำนวน UPS น้อยที่สุด แล้วเลือก runtime reserve ที่สูงกว่า'),
        ('E', 'Best Overall', 'จำนวน UPS น้อยที่สุด, ตามด้วยราคาที่เทียบกันได้บนฐานไม่รวม VAT, แล้ว runtime reserve'),
    ]
    for code, _, _ in scenario_specs:
        if code == 'A' and not price_comparable:
            continue
        ranked = sorted(solutions, key=lambda item: _rank_key(item, code, price_comparable))
        if code == 'C':
            ranked = [item for item in ranked if item['expansion']['maxBatteryPacks'] > 0]
        if ranked:
            best_by_scenario[code] = ranked[0]

    best = best_by_scenario.get('E')
    overall_ranked = sorted(solutions, key=lambda item: _rank_key(item, 'E', price_comparable))
    if best:
        overall_ranked = [item for item in overall_ranked if item['id'] != best['id']]
    alternatives = overall_ranked[:TOP_ALTERNATIVES]
    scenarios = []
    for code, name, objective in scenario_specs:
        if code == 'A' and not price_comparable:
            scenarios.append({
                'code': code, 'name': name, 'available': False,
                'reasonCode': 'incomparable_price_basis', 'reason': price_reason,
                'objective': objective, 'solution': None,
            })
            continue
        solution = best_by_scenario.get(code)
        if solution is None:
            reason = ('ไม่มีรุ่นที่ระบุจำนวน external battery packs สูงสุดไว้อย่างชัดเจน' if code == 'C'
                      else 'ไม่พบแบบที่ผ่านข้อจำกัด W, VA, Runtime และ Outlet ภายในขอบเขตการค้นหา')
            scenarios.append({
                'code': code, 'name': name, 'available': False,
                'reasonCode': 'no_feasible_design', 'reason': reason,
                'objective': objective, 'solution': None,
            })
            continue
        scenarios.append({
            'code': code, 'name': name, 'available': True,
            'reasonCode': None, 'reason': None,
            'objective': objective,
            'solution': {'id': solution['id'], 'unitCount': solution['unitCount'],
                         'modelMix': solution['models'],
                         'minimumEstimatedRuntimeMinutes': solution['minimumEstimatedRuntimeMinutes'],
                         'runtimeReserveMinutes': solution['runtimeReserveMinutes'],
                         'expansion': solution['expansion'],
                         'totalCost': solution['totalCost'],
                         'engineeringJustification': solution['engineeringJustification']},
        })

    notes = []
    comparable_currency = price_currency
    if reached_limit:
        notes.append('การค้นหาถึงเพดานจำนวนชุดที่กำหนดแล้ว แบบที่แสดงคือแบบดีที่สุดจากชุดที่ตรวจครบ')
    if not assignment_search_complete:
        notes.append('การจัดวางอุปกรณ์ถึงเพดานค้นหาหรือใช้ best-fit สำหรับอุปกรณ์เกิน 200 ชิ้น ผลเป็นแบบดีที่สุดจากตำแหน่งที่ตรวจแล้ว')
    if minimum_units > MAX_SEARCH_UNITS:
        notes.append(f'จำนวนขั้นต่ำจากโหลดรวมคือ {minimum_units} เครื่อง ซึ่งเกินขอบเขต solver ที่ {MAX_SEARCH_UNITS} เครื่อง')
    elif minimum_units > max_units_per_design:
        notes.append(f'ต้องใช้ UPS อย่างน้อย {minimum_units} เครื่อง แต่มีอุปกรณ์ที่จัดลง outlet ได้เพียง {parsed["device_count"]} ชิ้น; ระบบไม่แบ่งโหลดอุปกรณ์ชิ้นเดียวข้าม UPS')
    if not candidates:
        notes.append('ไม่พบ UPS ที่มีข้อมูลแหล่งอ้างอิงครบทั้ง W, VA, จำนวน outlet และ runtime curve')
    if len(alternatives) < TOP_ALTERNATIVES and best:
        notes.append(f'พบแบบทางเลือกที่แตกต่างและผ่านข้อกำหนด {len(alternatives)} แบบ')
    if price_comparable and missing_price_dates:
        notes.append(f'ราคา {missing_price_dates} รุ่นไม่มีวันที่ต้นทาง ควรตรวจสอบราคาก่อนสั่งซื้อ')
    if price_comparable and inclusive_offers_normalized:
        vat_period_text = ', '.join(f"{period['startsOn']} ถึง {period['endsOn']}" for period in vat_rate_periods)
        offer_period_text = ', '.join(f"{period['startsOn']} ถึง {period['endsOn']}"
                                      for period in price_effective_periods)
        notes.append(
            f'ปรับราคา inclusive {inclusive_offers_normalized} offers เป็นราคาไม่รวม VAT '
            f'ด้วยอัตรา {vat_rate_percent}% จากช่วงมีผลของราคา {offer_period_text} '
            f'ซึ่งอยู่ในช่วง VAT ที่ยืนยัน {vat_period_text}'
        )
    if missing_currency_offers:
        notes.append(f'ใช้ THB ตามการยืนยันของผู้ใช้กับ price offers {missing_currency_offers} รายการที่ไม่มี currency ใน catalog')
    if not best and candidates and not notes:
        notes.append('ไม่พบ fleet ที่ผ่านข้อจำกัดทั้งหมดในขอบเขตการค้นหานี้')
    return {
        'solver': 'CSP',
        'selectionDiagnostics': {'selectedProductIds': selected_ids,
            'selectedConfigurations': selected_configs,
            'excludedModels': excluded_selected, 'candidateModelCount': len(candidates)},
        'search': {'candidateModelCount': candidate_count, 'minimumUnitsByCapacity': minimum_units,
                   'maxUnitsPerDesign': max_units_per_design,
                   'configuredMaxUnitsPerDesign': MAX_SEARCH_UNITS,
                   'maxDistinctModelsPerDesign': MAX_DISTINCT_MODELS,
                   'fleetCandidatesVisited': fleet_candidates_visited,
                   'fleetCandidateBudget': work_budget,
                   'fleetEvaluations': fleet_evaluations, 'prunedFleetCandidates': pruned_fleets,
                   'assignmentNodes': assignment_nodes,
                   'maxAssignmentNodes': MAX_ASSIGNMENT_NODES_TOTAL,
                   'maxAssignmentNodesPerFleet': MAX_ASSIGNMENT_NODES_PER_FLEET,
                   'exactAssignmentInstanceLimit': MAX_EXACT_ASSIGNMENT_INSTANCES,
                   'isComplete': not reached_limit and assignment_search_complete
                   and minimum_units <= max_units_per_design},
        'priceComparison': {'available': price_comparable, 'currency': comparable_currency,
                            'currencySource': price_currency_source,
                            'offersMissingCurrency': missing_currency_offers,
                            'vatTreatment': comparable_vat, 'vatBasis': 'exclusive',
                            'inclusiveOffersNormalized': inclusive_offers_normalized,
                            'vatRatePercent': vat_rate_percent, 'vatRatePeriods': vat_rate_periods,
                            'priceEffectivePeriods': price_effective_periods,
                            'modelsMissingSourceDate': missing_price_dates,
                            'reasonCode': price_reason_code, 'reason': price_reason},
        'bestSolution': best,
        'topAlternatives': alternatives,
        'validDesignCount': len(solutions),
        'scenarios': scenarios,
        'notes': notes,
    }


def optimize(database_path, request, curve_catalog=None):
    """Open the local catalog read-only and run the bounded CSP optimizer."""
    path = Path(database_path)
    if not path.is_file():
        raise FileNotFoundError(f'UPS database not found: {path}')
    uri = path.as_uri() + '?mode=ro'
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        connection.row_factory = sqlite3.Row
        return optimize_from_connection(connection, request, curve_catalog=curve_catalog)
    finally:
        connection.close()
