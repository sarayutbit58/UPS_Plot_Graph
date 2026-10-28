"""Deterministic UPS fleet allocation with strict capacity, outlet, and runtime checks."""

from decimal import Decimal, InvalidOperation
from collections import deque
import math
from pathlib import Path
import re
import sqlite3

try:
    from .recommendation_engine import (
        MIN_ENGINEERING_MARGIN,
        _apply_manufacturer_curves,
        _build_inventory,
        _interpolate_runtime,
        _read_curve_catalog,
        _verified_battery_outlet_count,
        _verified_battery_outlet_groups,
        _verified_runtime_curve,
        _verified_rating,
        _verified_identity,
    )
except ImportError:  # Running this file or sibling API module directly.
    from recommendation_engine import (
        MIN_ENGINEERING_MARGIN,
        _apply_manufacturer_curves,
        _build_inventory,
        _interpolate_runtime,
        _read_curve_catalog,
        _verified_battery_outlet_count,
        _verified_battery_outlet_groups,
        _verified_runtime_curve,
        _verified_rating,
        _verified_identity,
    )


MAX_UPS_MODELS = 20
MAX_UPS_UNITS = 500
MAX_DEVICE_LINES = 500
MAX_DEVICE_INSTANCES = 10000
MAX_FIXED_SEARCH_DEVICES = 32
MAX_FIXED_SEARCH_UNITS = 16
MAX_FIXED_SEARCH_OUTLETS = 256
MAX_FIXED_SEARCH_NODES = 5000
CONNECTOR_OUTLETS = {'IEC_C14': 'IEC_C13', 'IEC_C20': 'IEC_C19',
                     'NEMA_5_15P': 'NEMA_5_15R'}


class AllocationInputError(ValueError):
    """Request fields cannot be used to perform a UPS allocation."""


def _decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None
    return number if number.is_finite() else None


def _number(value):
    number = _decimal(value)
    if number is None:
        return None
    try:
        result = float(number)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(result) or (number != 0 and result == 0):
        return None
    return result


def _finite_sum(values, label):
    total = sum((Decimal(str(value)) for value in values if value is not None), Decimal('0'))
    result = _number(total)
    if result is None:
        raise AllocationInputError(f'{label} exceeds the finite JSON number range')
    return result


def _integer(value, label, minimum, maximum):
    number = _decimal(value)
    if (number is None or number != number.to_integral_value()
            or number < minimum or number > maximum):
        raise AllocationInputError(f'{label} must be an integer from {minimum} to {maximum}')
    return int(number)


def _parse_request(request):
    if not isinstance(request, dict):
        raise AllocationInputError('Request body must be a JSON object')

    runtime_required = _decimal(request.get('runtimeRequirementMinutes'))
    runtime_required_number = _number(runtime_required)
    if runtime_required is None or runtime_required <= 0 or runtime_required_number is None:
        raise AllocationInputError('runtimeRequirementMinutes must be a finite number greater than 0')

    selection = request.get('selectedUps')
    if not isinstance(selection, dict):
        raise AllocationInputError('selectedUps must be an object')
    mode = str(selection.get('mode', '')).strip().casefold()
    if mode not in {'fixed', 'auto'}:
        raise AllocationInputError("selectedUps.mode must be 'fixed' or 'auto'")
    model_rows = selection.get('models')
    if not isinstance(model_rows, list) or not model_rows or len(model_rows) > MAX_UPS_MODELS:
        raise AllocationInputError(f'selectedUps.models must contain 1 to {MAX_UPS_MODELS} models')

    selected_models = []
    seen_selections = set()
    configs_by_product = {}
    initial_ups_count = 0
    for index, model_row in enumerate(model_rows):
        if not isinstance(model_row, dict):
            raise AllocationInputError(f'selectedUps.models[{index}] must be an object')
        product_id = _integer(model_row.get('productId'), f'selectedUps.models[{index}].productId', 1, 2**31 - 1)
        config_id = model_row.get('batteryConfigId')
        if config_id is not None and (not isinstance(config_id, str)
                or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}', config_id)):
            raise AllocationInputError(f'selectedUps.models[{index}].batteryConfigId is invalid')
        key = (product_id, config_id)
        previous_configs = configs_by_product.setdefault(product_id, set())
        if key in seen_selections or (previous_configs and (config_id is None or None in previous_configs)):
            raise AllocationInputError('selectedUps.models must not repeat a model and battery configuration')
        seen_selections.add(key)
        previous_configs.add(config_id)
        if mode == 'fixed' and 'quantity' not in model_row:
            raise AllocationInputError(f'selectedUps.models[{index}].quantity is required in fixed mode')
        quantity = _integer(model_row.get('quantity', 0), f'selectedUps.models[{index}].quantity',
                            1 if mode == 'fixed' else 0, MAX_UPS_UNITS)
        initial_ups_count += quantity
        selected_models.append({'product_id': product_id, 'battery_config_id': config_id,
                                'quantity': quantity, 'selection_index': index})
    if initial_ups_count > MAX_UPS_UNITS:
        raise AllocationInputError(f'Initial UPS count must not exceed {MAX_UPS_UNITS}')

    devices = request.get('devices')
    if not isinstance(devices, list) or not devices or len(devices) > MAX_DEVICE_LINES:
        raise AllocationInputError(f'devices must contain 1 to {MAX_DEVICE_LINES} entries')
    parsed_devices = []
    total_device_instances = 0
    total_outlet_instances = 0
    total_watts = Decimal('0')
    total_va = Decimal('0')
    for index, device in enumerate(devices):
        if not isinstance(device, dict):
            raise AllocationInputError(f'devices[{index}] must be an object')
        name = re.sub(r'\s+', ' ', str(device.get('name') or '')).strip()
        if not name or len(name) > 160:
            raise AllocationInputError(f'devices[{index}].name must contain 1 to 160 characters')
        quantity = _integer(device.get('quantity'), f'devices[{index}].quantity', 1, MAX_DEVICE_INSTANCES)
        watts = _decimal(device.get('watt', device.get('watts')))
        va = _decimal(device.get('va'))
        if watts is None or watts < 0 or _number(watts) is None:
            raise AllocationInputError(f'devices[{index}].watt must be a finite non-negative number')
        if va is None or va < 0 or _number(va) is None:
            raise AllocationInputError(f'devices[{index}].va must be a finite non-negative number')
        if (watts == 0) != (va == 0):
            raise AllocationInputError(f'devices[{index}] must specify both W and VA as zero or both positive')
        priority = _integer(device.get('priority'), f'devices[{index}].priority', 1, 100)
        plugs = device.get('plugs', [{'connector': None, 'count': 1}])
        if not isinstance(plugs, list) or not plugs or len(plugs) > 100:
            raise AllocationInputError(f'devices[{index}].plugs must contain 1 to 100 groups')
        parsed_plugs = []
        for plug_index, plug in enumerate(plugs):
            if not isinstance(plug, dict):
                raise AllocationInputError(f'devices[{index}].plugs[{plug_index}] must be an object')
            connector = plug.get('connector')
            if connector is not None and (not isinstance(connector, str) or connector not in CONNECTOR_OUTLETS):
                raise AllocationInputError(f'devices[{index}].plugs[{plug_index}].connector is unsupported')
            count = _integer(plug.get('count'), f'devices[{index}].plugs[{plug_index}].count', 1,
                             MAX_DEVICE_INSTANCES)
            parsed_plugs.append({'connector': connector, 'count': count})
        plug_count = sum(plug['count'] for plug in parsed_plugs)
        total_outlet_instances += quantity * plug_count
        if total_outlet_instances > MAX_DEVICE_INSTANCES:
            raise AllocationInputError(f'Total plug quantity must not exceed {MAX_DEVICE_INSTANCES}')
        total_device_instances += quantity
        if total_device_instances > MAX_DEVICE_INSTANCES:
            raise AllocationInputError(f'Total device quantity must not exceed {MAX_DEVICE_INSTANCES}')
        total_watts += watts * quantity
        total_va += va * quantity
        parsed_devices.append({
            'device_index': index, 'name': name, 'quantity': quantity,
            'watt_each': float(watts), 'va_each': float(va), 'priority': priority,
            'plugs': parsed_plugs, 'plug_count': plug_count,
        })

    total_watts_number = _number(total_watts)
    total_va_number = _number(total_va)
    if total_watts_number is None or total_va_number is None:
        raise AllocationInputError('Total load exceeds the supported numeric range')
    if total_watts_number <= 0 or total_va_number <= 0:
        raise AllocationInputError('At least one device must have a positive watt and VA load')

    return {
        'mode': mode, 'selected_models': selected_models,
        'runtime_required': runtime_required_number,
        'devices': parsed_devices, 'device_count': total_device_instances,
        'required_watts': total_watts_number, 'required_va': total_va_number,
        'required_outlets': total_outlet_instances,
    }


def _model_records(connection, selected_models, curve_catalog):
    if connection.row_factory is None:
        connection.row_factory = sqlite3.Row
    inventory = _build_inventory(connection)
    _apply_manufacturer_curves(inventory, _read_curve_catalog(curve_catalog))
    by_id = {product['id']: product for product in inventory}
    models = []
    for selected in selected_models:
        product = by_id.get(selected['product_id'])
        if product is None or product.get('non_ups_reason'):
            raise AllocationInputError(f"Selected productId {selected['product_id']} is not an allocatable UPS")
        missing = []
        if not _verified_identity(product):
            missing.append('identity_unverified')
        rating_w = _verified_rating(product, 'rating_w')
        rating_va = _verified_rating(product, 'rating_va')
        if rating_w is None:
            missing.append('watt_capacity_unknown')
        if rating_va is None:
            missing.append('va_capacity_unknown')
        backed_outlets = _verified_battery_outlet_count(product)
        if backed_outlets is None:
            missing.append('outlet_unknown')
        curve = _verified_runtime_curve(product, selected.get('battery_config_id'))
        if curve is None:
            missing.append('runtime_unknown')
        models.append({
            'product_id': product['id'], 'manufacturer': product['manufacturer'],
            'model': product['model'], 'sku': product['sku'],
            'identity_verified': _verified_identity(product),
            'battery_config_id': curve['configurationId'] if curve else selected.get('battery_config_id'),
            'rating_w': rating_w, 'rating_va': rating_va,
            'rating_w_source': product.get('rating_w_source') if rating_w is not None else None,
            'rating_va_source': product.get('rating_va_source') if rating_va is not None else None,
            'identity_source': product.get('identity_source') if _verified_identity(product) else None,
            'outlet_count': backed_outlets, 'runtime_curve': curve,
            'outlet_groups': _verified_battery_outlet_groups(product),
            'quantity': selected['quantity'], 'selection_index': selected['selection_index'],
            'evidence': list(product.get('evidence', [])),
            'source_warnings': [warning for warning in product.get('warnings', [])
                                if 'price' not in warning.casefold()],
            'manufacturer_outlet_source': product.get('manufacturer_outlet_source'),
            'unusable_reasons': missing,
        })
    return models


def _evidence_refs(model, flag):
    refs = []
    seen = set()
    for evidence in model.get('evidence', []):
        if not evidence.get(flag):
            continue
        reference = {'path': evidence.get('path'), 'locator': evidence.get('locator')}
        key = (reference['path'], reference['locator'])
        if key not in seen:
            seen.add(key)
            refs.append(reference)
        if len(refs) >= 3:
            break
    return refs


def _technical_evidence(model):
    outlets = [group['source'] for group in model.get('outlet_groups') or []
               if group.get('source')]
    curve = model.get('runtime_curve')
    return {
        'identity': [model['identity_source']] if model.get('identity_source') else [],
        'ratingWatts': [model['rating_w_source']] if model.get('rating_w_source') else [],
        'ratingVa': [model['rating_va_source']] if model.get('rating_va_source') else [],
        'outlets': outlets,
        'runtimeCurve': [] if not curve else [curve.get('source')],
    }


def _new_unit(model, sequence, auto_added):
    return {
        'ups_unit_id': f'UPS-{sequence:02d}', 'sequence': sequence,
        'product_id': model['product_id'], 'manufacturer': model['manufacturer'],
        'model': model['model'], 'sku': model['sku'],
        'identity_verified': model.get('identity_verified') is True,
        'battery_config_id': model.get('battery_config_id'),
        'rating_w': model['rating_w'], 'rating_va': model['rating_va'],
        'rating_w_source': model.get('rating_w_source'), 'rating_va_source': model.get('rating_va_source'),
        'identity_source': model.get('identity_source'),
        'outlet_count': model['outlet_count'], 'runtime_curve': model['runtime_curve'],
        'outlet_groups': model.get('outlet_groups'),
        'evidence': model['evidence'], 'source_warnings': model['source_warnings'],
        'manufacturer_outlet_source': model['manufacturer_outlet_source'],
        'auto_added': auto_added, 'used_w': 0.0, 'used_va': 0.0, 'outlets': [],
        'plug_demands': [], 'device_refs': [],
    }


def _plan_outlets(unit, device=None):
    """Match all plugs on a unit, preserving typed sockets for typed plugs."""
    if unit.get('outlet_count') is None or unit.get('outlet_groups') is None:
        return None, 'outlet_unknown'
    free_groups = []
    number = 0
    for group in unit['outlet_groups']:
        end = number + group['count']
        free_groups.append({'outletType': group.get('type'), 'numbers': deque(range(number + 1, end + 1))})
        number = end
    demands = list(unit['plug_demands'])
    if device is not None:
        demands.append(device['plugs'])
    needed = sum(plug['count'] for groups in demands for plug in groups)
    if number < needed:
        return None, 'outlet_capacity'
    assigned = [[] for _ in demands]
    flattened = [(index, plug['connector']) for index, groups in enumerate(demands)
                 for plug in groups for _ in range(plug['count'])]
    # Re-evaluate earlier untyped plugs whenever another device is placed.
    # This avoids consuming the only C13 socket before a later C14 plug arrives.
    for owner_index, connector in sorted(flattened, key=lambda item: item[1] is None):
        required_type = CONNECTOR_OUTLETS.get(connector) if connector else None
        match = next((group for group in free_groups if group['numbers']
                      and (required_type is None or group['outletType'] == required_type)), None)
        if match is None:
            unknown = any(group['numbers'] and (not group['outletType']
                          or group['outletType'] in {'UNIVERSAL', 'AS'}) for group in free_groups)
            return None, 'connector_unknown' if unknown else 'connector_mismatch'
        assigned[owner_index].append({
            'outletNumber': match['numbers'].popleft(), 'outletType': match['outletType'],
            'connector': connector, 'compatibilityStatus': 'verified' if connector else 'not_checked',
        })
    return assigned, None


def _commit_outlets(unit, device):
    plan, reason = _plan_outlets(unit, device)
    if reason:
        raise AllocationInputError('Outlet assignment changed unexpectedly')
    unit['plug_demands'].append(device['plugs'])
    unit['outlets'] = [slot['outletNumber'] for assignment in plan for slot in assignment]


def _rollback_outlets(unit):
    unit['plug_demands'].pop()
    plan, reason = _plan_outlets(unit)
    if reason:
        raise AllocationInputError('Outlet rollback changed unexpectedly')
    unit['outlets'] = [slot['outletNumber'] for assignment in plan for slot in assignment]


def _placement(unit, device, runtime_required, allow_partial_below_curve=False):
    watts = unit['used_w'] + device['watt_each']
    va = unit['used_va'] + device['va_each']
    outlet_number = len(unit['outlets']) + device['plug_count']
    reasons = []
    if not unit.get('identity_verified'):
        reasons.append('identity_unverified')

    rating_w = unit['rating_w']
    rating_va = unit['rating_va']
    if rating_w is None:
        reasons.append('watt_capacity_unknown')
    else:
        planning_limit_w = rating_w / (1 + float(MIN_ENGINEERING_MARGIN))
        if watts > rating_w + 1e-9:
            reasons.append('watt_overload')
        elif watts > planning_limit_w + 1e-9:
            reasons.append('watt_capacity')
    if rating_va is None:
        reasons.append('va_capacity_unknown')
    else:
        planning_limit_va = rating_va / (1 + float(MIN_ENGINEERING_MARGIN))
        if va > rating_va + 1e-9:
            reasons.append('va_overload')
        elif va > planning_limit_va + 1e-9:
            reasons.append('va_capacity')
    _, outlet_reason = _plan_outlets(unit, device)
    if outlet_reason:
        reasons.append(outlet_reason)

    runtime = None
    curve = unit['runtime_curve']
    if curve is None:
        reasons.append('runtime_unknown')
    elif watts > 0:
        runtime = _interpolate_runtime(curve['points'], watts)
        if runtime is None:
            below_range = watts < min(point['loadW'] for point in curve['points'])
            if not (allow_partial_below_curve and below_range):
                reasons.append('runtime_unknown')
        elif runtime + 1e-9 < runtime_required:
            reasons.append('runtime_insufficient')

    if reasons:
        return reasons, runtime, None
    safe_w = rating_w / (1 + float(MIN_ENGINEERING_MARGIN))
    safe_va = rating_va / (1 + float(MIN_ENGINEERING_MARGIN))
    utilization = max(watts / safe_w, va / safe_va, outlet_number / unit['outlet_count'])
    return [], runtime, utilization


def _record_placement(unit, device, instance_number, runtime, allocations):
    unit['used_w'] += device['watt_each']
    unit['used_va'] += device['va_each']
    _commit_outlets(unit, device)
    allocation = {
        'deviceIndex': device['device_index'], 'instanceNumber': instance_number,
        'name': device['name'], 'priority': device['priority'], 'quantity': 1,
        'watt': device['watt_each'], 'va': device['va_each'],
        'upsUnitId': unit['ups_unit_id'], 'productId': unit['product_id'],
        'model': unit['model'], 'outletNumber': None, 'outletNumbers': [],
        'estimatedRuntimeMinutes': None if runtime is None else round(runtime, 2),
        'runtimeStatus': 'not_applicable_zero_load' if runtime is None else 'meets_requirement',
    }
    allocations.append(allocation)
    unit['device_refs'].append(allocation)


def _fixed_units(models):
    result = []
    for model in models:
        for _ in range(model['quantity']):
            result.append(_new_unit(model, len(result) + 1, auto_added=False))
    return result


def _fixed_search(models, ordered_devices, parsed, current_units):
    """Find a complete fixed-fleet assignment, or explicitly report a search limit."""
    if parsed['device_count'] > MAX_FIXED_SEARCH_DEVICES:
        return None, False, 'fixed_search_device_limit'
    if len(current_units) > MAX_FIXED_SEARCH_UNITS:
        return None, False, 'fixed_search_unit_limit'
    if sum(unit['outlet_count'] or 0 for unit in current_units) > MAX_FIXED_SEARCH_OUTLETS:
        return None, False, 'fixed_search_outlet_limit'

    units = _fixed_units(models)
    instances = [(device, number) for device in ordered_devices
                 for number in range(1, device['quantity'] + 1)]
    assignment = []
    nodes = 0
    limited = False

    def search(index):
        nonlocal nodes, limited
        if nodes >= MAX_FIXED_SEARCH_NODES:
            limited = True
            return None
        nodes += 1
        if index == len(instances):
            for unit in units:
                if not unit['used_w']:
                    continue
                curve = unit['runtime_curve']
                runtime = _interpolate_runtime(curve['points'], unit['used_w']) if curve else None
                if runtime is None or runtime + 1e-9 < parsed['runtime_required']:
                    return None
            return list(assignment)

        device, _ = instances[index]
        viable = []
        for unit_index, unit in enumerate(units):
            reasons, runtime, utilization = _placement(
                unit, device, parsed['runtime_required'], allow_partial_below_curve=True)
            if not reasons:
                viable.append((utilization, unit['sequence'], unit_index, runtime))
        viable.sort(key=lambda item: (
            item[3] is None and units[item[2]]['used_w'] + device['watt_each'] > 0,
            -item[0], item[1]))
        for _, _, unit_index, _ in viable:
            unit = units[unit_index]
            old_w, old_va = unit['used_w'], unit['used_va']
            unit['used_w'] += device['watt_each']
            unit['used_va'] += device['va_each']
            _commit_outlets(unit, device)
            assignment.append(unit_index)
            found = search(index + 1)
            assignment.pop()
            _rollback_outlets(unit)
            unit['used_w'], unit['used_va'] = old_w, old_va
            if found is not None:
                return found
            if limited:
                return None
        return None

    answer = search(0)
    if answer is not None:
        return (instances, answer), True, 'feasible_assignment_found'
    if limited:
        return None, False, 'fixed_search_node_limit'
    return None, True, 'fixed_search_exhausted'


def _distinct_reasons(groups):
    ordered = []
    for reasons in groups:
        for reason in reasons:
            if reason not in ordered:
                ordered.append(reason)
    return ordered


def _recommendations(validation, capacity_report):
    recommendations = []
    reasons = {reason for issue in validation['issues'] for reason in issue['reasons']}
    if capacity_report['overloaded'] or reasons & {'watt_capacity', 'watt_overload', 'va_capacity', 'va_overload'}:
        recommendations.append({
            'code': 'increase_ups_capacity',
            'message': 'Add a selected UPS unit or select a UPS model with higher W/VA ratings while preserving the 20% engineering margin.',
        })
    if reasons & {'outlet_capacity', 'outlet_unknown', 'connector_unknown', 'connector_mismatch'}:
        recommendations.append({
            'code': 'increase_verified_outlets',
            'message': 'Add a selected UPS with a verified outlet count or select a model with more source-listed outlets.',
        })
    if reasons & {'runtime_insufficient', 'runtime_unknown'}:
        recommendations.append({
            'code': 'select_verified_runtime',
            'message': 'Select a UPS with a source runtime curve that covers the allocated watt load and meets the requested runtime.',
        })
    if reasons & {'watt_capacity_unknown', 'va_capacity_unknown'}:
        recommendations.append({
            'code': 'verify_capacity_data',
            'message': 'Use a UPS with both W and VA ratings recorded in the catalog before allocating loads.',
        })
    return recommendations


def allocate_from_connection(connection, request, curve_catalog=None):
    """Allocate device instances across fixed UPS units or an auto-added selected-model pool."""
    parsed = _parse_request(request)
    models = _model_records(connection, parsed['selected_models'], curve_catalog)
    units = []
    for model in models:
        for _ in range(model['quantity']):
            units.append(_new_unit(model, len(units) + 1, auto_added=False))

    device_allocations = []
    outlet_allocations = []
    unassigned_instances = []
    ordered_devices = sorted(parsed['devices'], key=lambda item: (-item['priority'], item['device_index']))
    for device in ordered_devices:
        for instance_number in range(1, device['quantity'] + 1):
            viable = []
            current_reasons = []
            for unit in units:
                reasons, runtime, utilization = _placement(unit, device, parsed['runtime_required'],
                                                           allow_partial_below_curve=True)
                if reasons:
                    current_reasons.append(reasons)
                else:
                    viable.append((utilization, unit['sequence'], unit, runtime))

            if viable:
                _, _, unit, runtime = min(viable, key=lambda item: (
                    item[3] is None and item[2]['used_w'] + device['watt_each'] > 0,
                    -item[0], item[1]))
            elif parsed['mode'] == 'auto' and len(units) < MAX_UPS_UNITS:
                new_options = []
                model_reasons = []
                for model in models:
                    if model['unusable_reasons']:
                        model_reasons.append(model['unusable_reasons'])
                        continue
                    candidate = _new_unit(model, len(units) + 1, auto_added=True)
                    reasons, runtime, utilization = _placement(candidate, device, parsed['runtime_required'],
                                                               allow_partial_below_curve=True)
                    if reasons:
                        model_reasons.append(reasons)
                    else:
                        new_options.append((utilization, model['selection_index'], model, runtime))
                if new_options:
                    _, _, model, runtime = min(new_options, key=lambda item: (
                        item[3] is None and device['watt_each'] > 0, -item[0], item[1]))
                    unit = _new_unit(model, len(units) + 1, auto_added=True)
                    units.append(unit)
                else:
                    reasons = _distinct_reasons(current_reasons + model_reasons)
                    if not reasons:
                        reasons = ['no_usable_selected_ups']
                    unassigned_instances.append({
                        'device_index': device['device_index'], 'instance_number': instance_number,
                        'device': device, 'reasons': reasons,
                    })
                    continue
            else:
                reasons = _distinct_reasons(current_reasons)
                if parsed['mode'] == 'auto' and len(units) >= MAX_UPS_UNITS:
                    reasons.append('unit_limit_reached')
                if not reasons:
                    for model in models:
                        reasons.extend(model['unusable_reasons'])
                    reasons = list(dict.fromkeys(reasons)) or ['no_usable_selected_ups']
                unassigned_instances.append({
                    'device_index': device['device_index'], 'instance_number': instance_number,
                    'device': device, 'reasons': reasons,
                })
                continue

            _record_placement(unit, device, instance_number, runtime, device_allocations)

    devices_by_index = {device['device_index']: device for device in parsed['devices']}
    for unit in units:
        if not unit['used_w']:
            continue
        runtime = (_interpolate_runtime(unit['runtime_curve']['points'], unit['used_w'])
                   if unit['runtime_curve'] else None)
        if runtime is not None and runtime + 1e-9 >= parsed['runtime_required']:
            continue
        reason = 'runtime_unknown' if runtime is None else 'runtime_insufficient'
        for allocation in unit['device_refs']:
            device = devices_by_index[allocation['deviceIndex']]
            unassigned_instances.append({'device_index': device['device_index'],
                'instance_number': allocation['instanceNumber'], 'device': device, 'reasons': [reason]})
            device_allocations.remove(allocation)
        unit['used_w'] = 0.0
        unit['used_va'] = 0.0
        unit['outlets'] = []
        unit['plug_demands'] = []
        unit['device_refs'] = []

    search_complete = True
    search_reason = 'feasible_assignment_found'
    if parsed['mode'] == 'fixed' and unassigned_instances:
        solution, search_complete, search_reason = _fixed_search(models, ordered_devices, parsed, units)
        if solution is not None:
            instances, assignment = solution
            units = _fixed_units(models)
            device_allocations = []
            unassigned_instances = []
            for (device, instance_number), unit_index in zip(instances, assignment):
                unit = units[unit_index]
                reasons, runtime, _ = _placement(unit, device, parsed['runtime_required'],
                                                  allow_partial_below_curve=True)
                if reasons:
                    raise AllocationInputError('Fixed allocation changed unexpectedly')
                _record_placement(unit, device, instance_number, runtime, device_allocations)
    elif parsed['mode'] == 'auto' and unassigned_instances:
        search_complete = False
        search_reason = 'auto_search_not_exhaustive'

    for unit in units:
        if not unit['device_refs']:
            continue
        plan, outlet_error = _plan_outlets(unit)
        if outlet_error or len(plan) != len(unit['device_refs']):
            raise AllocationInputError('Final outlet assignment is inconsistent')
        for allocation, assigned_outlets in zip(unit['device_refs'], plan):
            numbers = [outlet['outletNumber'] for outlet in assigned_outlets]
            allocation['outletNumber'] = numbers[0]
            allocation['outletNumbers'] = numbers
            for outlet in assigned_outlets:
                outlet_allocations.append({
                    'upsUnitId': unit['ups_unit_id'], 'productId': unit['product_id'],
                    **outlet,
                    'compatibilityEvidence': ('connector and outlet type matched' if outlet['connector']
                                              else 'connector compatibility was not checked'),
                    'deviceIndex': allocation['deviceIndex'], 'instanceNumber': allocation['instanceNumber'],
                    'deviceName': allocation['name'],
                })

    unassigned_groups = {}
    for item in unassigned_instances:
        key = (item['device_index'], tuple(item['reasons']))
        group = unassigned_groups.setdefault(key, {
            'deviceIndex': item['device_index'], 'name': item['device']['name'],
            'priority': item['device']['priority'], 'watt': item['device']['watt_each'],
            'va': item['device']['va_each'], 'quantity': 0,
            'unassignedInstanceNumbers': [], 'reasons': list(item['reasons']),
        })
        group['quantity'] += 1
        group['unassignedInstanceNumbers'].append(item['instance_number'])
    unassigned_devices = sorted(unassigned_groups.values(), key=lambda item: item['deviceIndex'])

    final_runtime_by_unit = {}
    for unit in units:
        runtime = (_interpolate_runtime(unit['runtime_curve']['points'], unit['used_w'])
                   if unit['used_w'] and unit['runtime_curve'] else None)
        if unit['used_w'] and runtime is None:
            raise AllocationInputError('Final UPS load is outside the selected runtime curve range')
        final_runtime_by_unit[unit['ups_unit_id']] = runtime
    for allocation in device_allocations:
        runtime = final_runtime_by_unit[allocation['upsUnitId']]
        allocation['estimatedRuntimeMinutes'] = None if runtime is None else round(runtime, 2)
        allocation['runtimeStatus'] = 'not_applicable_zero_load' if runtime is None else 'meets_requirement'

    assigned_w = _finite_sum((unit['used_w'] for unit in units), 'Assigned W')
    assigned_va = _finite_sum((unit['used_va'] for unit in units), 'Assigned VA')
    rated_w = _finite_sum((unit['rating_w'] for unit in units), 'Selected UPS W capacity')
    rated_va = _finite_sum((unit['rating_va'] for unit in units), 'Selected UPS VA capacity')
    planning_w = rated_w / (1 + float(MIN_ENGINEERING_MARGIN))
    planning_va = rated_va / (1 + float(MIN_ENGINEERING_MARGIN))
    maximum_single_w = max((model['rating_w'] for model in models if model['rating_w'] is not None), default=0)
    maximum_single_va = max((model['rating_va'] for model in models if model['rating_va'] is not None), default=0)
    unknown_single_w = any(model['rating_w'] is None for model in models)
    unknown_single_va = any(model['rating_va'] is None for model in models)
    single_device_overload_w = not unknown_single_w and any(
        device['watt_each'] > maximum_single_w + 1e-9 for device in parsed['devices'])
    single_device_overload_va = not unknown_single_va and any(
        device['va_each'] > maximum_single_va + 1e-9 for device in parsed['devices'])
    unknown_fleet_w = any(unit['rating_w'] is None for unit in units)
    unknown_fleet_va = any(unit['rating_va'] is None for unit in units)
    fixed_fleet_overload_w = (parsed['mode'] == 'fixed' and not unknown_fleet_w
                              and parsed['required_watts'] > rated_w + 1e-9)
    fixed_fleet_overload_va = (parsed['mode'] == 'fixed' and not unknown_fleet_va
                               and parsed['required_va'] > rated_va + 1e-9)
    overloaded_w = single_device_overload_w or fixed_fleet_overload_w
    overloaded_va = single_device_overload_va or fixed_fleet_overload_va
    reasons = {reason for group in unassigned_devices for reason in group['reasons']}
    capacity_valid = not bool(reasons & {
        'watt_capacity', 'watt_overload', 'va_capacity', 'va_overload',
        'watt_capacity_unknown', 'va_capacity_unknown',
    })
    outlet_valid = not bool(reasons & {'outlet_capacity', 'outlet_unknown',
                                       'connector_unknown', 'connector_mismatch'})
    runtime_valid = not bool(reasons & {'runtime_insufficient', 'runtime_unknown'})
    all_assigned = not unassigned_devices
    connectors = [plug['connector'] for device in parsed['devices'] for plug in device['plugs']]
    connector_status = ('not_checked' if all(connector is None for connector in connectors)
                        else 'not_verified' if not outlet_valid or not all_assigned
                        else 'partial' if any(connector is None for connector in connectors)
                        else 'verified')
    validation = {
        'isValid': all_assigned and capacity_valid and outlet_valid and runtime_valid,
        'allDevicesAssigned': all_assigned,
        'searchComplete': search_complete,
        'searchReason': search_reason,
        'connectorStatus': connector_status,
        'capacityValid': capacity_valid,
        'outletValid': outlet_valid,
        'runtimeValid': runtime_valid,
        'issues': [
            {'deviceIndex': group['deviceIndex'], 'deviceName': group['name'],
             'quantity': group['quantity'], 'reasons': group['reasons']}
            for group in unassigned_devices
        ],
    }
    capacity_report = {
        'requiredLoad': {'watts': parsed['required_watts'], 'va': parsed['required_va'],
                         'deviceCount': parsed['device_count']},
        'fleetCapacity': {'upsUnits': len(units), 'ratedWatts': round(rated_w, 2),
                          'ratedVa': round(rated_va, 2), 'planningLimitWatts': round(planning_w, 2),
                          'planningLimitVa': round(planning_va, 2),
                          'maximumSingleUpsWatts': maximum_single_w or None,
                          'maximumSingleUpsVa': maximum_single_va or None,
                          'minimumEngineeringMarginPercent': float(MIN_ENGINEERING_MARGIN * 100)},
        'assignedLoad': {'watts': round(assigned_w, 2), 'va': round(assigned_va, 2)},
        'overloadedWatts': overloaded_w, 'overloadedVa': overloaded_va,
        'overloaded': overloaded_w or overloaded_va,
        'engineeringMarginInsufficient': bool(reasons & {'watt_capacity', 'va_capacity'}) or (
            parsed['mode'] == 'fixed' and not unknown_fleet_w and not unknown_fleet_va
            and (parsed['required_watts'] > planning_w + 1e-9
                 or parsed['required_va'] > planning_va + 1e-9)
        ),
    }

    unit_summaries = []
    for unit in units:
        runtime = final_runtime_by_unit[unit['ups_unit_id']]
        unit_summaries.append({
            'upsUnitId': unit['ups_unit_id'], 'productId': unit['product_id'],
            'manufacturer': unit['manufacturer'], 'model': unit['model'], 'sku': unit['sku'],
            'batteryConfigId': unit['battery_config_id'],
            'autoAdded': unit['auto_added'], 'ratings': {'watts': unit['rating_w'], 'va': unit['rating_va']},
            'usedLoad': {'watts': round(unit['used_w'], 2), 'va': round(unit['used_va'], 2)},
            'utilizationPercent': {
                'watts': None if not unit['rating_w'] else round(unit['used_w'] / unit['rating_w'] * 100, 2),
                'va': None if not unit['rating_va'] else round(unit['used_va'] / unit['rating_va'] * 100, 2),
            },
            'outlets': {'used': len(unit['outlets']), 'available': unit['outlet_count'],
                        'assignedNumbers': list(unit['outlets'])},
            'estimatedRuntimeMinutes': None if runtime is None else round(runtime, 2),
            'runtimeStatus': ('not_applicable_zero_load' if unit['outlets'] and not unit['used_w']
                              else 'unused' if not unit['used_w'] else 'meets_requirement'),
            'runtimeCurveSource': None if not unit['runtime_curve'] else unit['runtime_curve']['source'],
            'technicalEvidence': _technical_evidence(unit),
            'sourceWarnings': list(unit['source_warnings']),
        })

    return {
        'inventoryMode': parsed['mode'],
        'searchComplete': search_complete,
        'searchReason': search_reason,
        'runtimeRequirementMinutes': parsed['runtime_required'],
        'deviceAllocations': device_allocations,
        'unassignedDevices': unassigned_devices,
        'upsUtilization': unit_summaries,
        'outletAllocations': outlet_allocations,
        'capacityReport': capacity_report,
        'validation': validation,
        'engineeringRecommendations': _recommendations(validation, capacity_report),
        'selectedModels': [
            {'productId': model['product_id'], 'manufacturer': model['manufacturer'],
             'model': model['model'], 'sku': model['sku'], 'initialQuantity': model['quantity'],
             'batteryConfigId': model['battery_config_id'],
             'unusableReasons': model['unusable_reasons'],
             'technicalEvidence': _technical_evidence(model),
             'sourceWarnings': list(model['source_warnings'])}
            for model in models
        ],
    }


def allocate(database_path, request, curve_catalog=None):
    """Open the local catalog read-only and allocate against the selected UPS models."""
    path = Path(database_path)
    if not path.is_file():
        raise FileNotFoundError(f'UPS database not found: {path}')
    connection = sqlite3.connect(str(path), timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only = ON')
    try:
        return allocate_from_connection(connection, request, curve_catalog)
    finally:
        connection.close()
