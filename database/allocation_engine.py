"""Deterministic UPS fleet allocation with strict capacity, outlet, and runtime checks."""

from decimal import Decimal, InvalidOperation
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
    )
except ImportError:  # Running this file or sibling API module directly.
    from recommendation_engine import (
        MIN_ENGINEERING_MARGIN,
        _apply_manufacturer_curves,
        _build_inventory,
        _interpolate_runtime,
        _read_curve_catalog,
    )


MAX_UPS_MODELS = 20
MAX_UPS_UNITS = 500
MAX_DEVICE_LINES = 500
MAX_DEVICE_INSTANCES = 10000


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
    seen_product_ids = set()
    initial_ups_count = 0
    for index, model_row in enumerate(model_rows):
        if not isinstance(model_row, dict):
            raise AllocationInputError(f'selectedUps.models[{index}] must be an object')
        product_id = _integer(model_row.get('productId'), f'selectedUps.models[{index}].productId', 1, 2**31 - 1)
        if product_id in seen_product_ids:
            raise AllocationInputError('selectedUps.models must not repeat productId values')
        seen_product_ids.add(product_id)
        if mode == 'fixed' and 'quantity' not in model_row:
            raise AllocationInputError(f'selectedUps.models[{index}].quantity is required in fixed mode')
        quantity = _integer(model_row.get('quantity', 0), f'selectedUps.models[{index}].quantity',
                            1 if mode == 'fixed' else 0, MAX_UPS_UNITS)
        initial_ups_count += quantity
        selected_models.append({'product_id': product_id, 'quantity': quantity, 'selection_index': index})
    if initial_ups_count > MAX_UPS_UNITS:
        raise AllocationInputError(f'Initial UPS count must not exceed {MAX_UPS_UNITS}')

    devices = request.get('devices')
    if not isinstance(devices, list) or not devices or len(devices) > MAX_DEVICE_LINES:
        raise AllocationInputError(f'devices must contain 1 to {MAX_DEVICE_LINES} entries')
    parsed_devices = []
    total_device_instances = 0
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
        if watts is None or watts <= 0 or _number(watts) is None:
            raise AllocationInputError(f'devices[{index}].watt must be a finite number greater than 0')
        if va is None or va <= 0 or _number(va) is None:
            raise AllocationInputError(f'devices[{index}].va must be a finite number greater than 0')
        priority = _integer(device.get('priority'), f'devices[{index}].priority', 1, 100)
        total_device_instances += quantity
        if total_device_instances > MAX_DEVICE_INSTANCES:
            raise AllocationInputError(f'Total device quantity must not exceed {MAX_DEVICE_INSTANCES}')
        total_watts += watts * quantity
        total_va += va * quantity
        parsed_devices.append({
            'device_index': index, 'name': name, 'quantity': quantity,
            'watt_each': float(watts), 'va_each': float(va), 'priority': priority,
        })

    total_watts_number = _number(total_watts)
    total_va_number = _number(total_va)
    if total_watts_number is None or total_va_number is None:
        raise AllocationInputError('Total load exceeds the supported numeric range')

    return {
        'mode': mode, 'selected_models': selected_models,
        'runtime_required': runtime_required_number,
        'devices': parsed_devices, 'device_count': total_device_instances,
        'required_watts': total_watts_number, 'required_va': total_va_number,
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
        if product.get('rating_w') is None:
            missing.append('watt_capacity_unknown')
        if product.get('rating_va') is None:
            missing.append('va_capacity_unknown')
        if product.get('outlet_count') is None:
            missing.append('outlet_unknown')
        if product.get('runtime_curve') is None:
            missing.append('runtime_unknown')
        models.append({
            'product_id': product['id'], 'manufacturer': product['manufacturer'],
            'model': product['model'], 'sku': product['sku'],
            'rating_w': product.get('rating_w'), 'rating_va': product.get('rating_va'),
            'outlet_count': product.get('outlet_count'), 'runtime_curve': product.get('runtime_curve'),
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
    outlet_source = model.get('manufacturer_outlet_source')
    outlets = [outlet_source] if outlet_source else _evidence_refs(model, 'outletEvidence')
    curve = model.get('runtime_curve')
    return {
        'ratingWatts': _evidence_refs(model, 'ratingWEvidence'),
        'ratingVa': _evidence_refs(model, 'ratingVaEvidence'),
        'outlets': outlets,
        'runtimeCurve': [] if not curve else [curve.get('source')],
    }


def _new_unit(model, sequence, auto_added):
    return {
        'ups_unit_id': f'UPS-{sequence:02d}', 'sequence': sequence,
        'product_id': model['product_id'], 'manufacturer': model['manufacturer'],
        'model': model['model'], 'sku': model['sku'],
        'rating_w': model['rating_w'], 'rating_va': model['rating_va'],
        'outlet_count': model['outlet_count'], 'runtime_curve': model['runtime_curve'],
        'evidence': model['evidence'], 'source_warnings': model['source_warnings'],
        'manufacturer_outlet_source': model['manufacturer_outlet_source'],
        'auto_added': auto_added, 'used_w': 0.0, 'used_va': 0.0, 'outlets': [],
    }


def _placement(unit, device, runtime_required):
    watts = unit['used_w'] + device['watt_each']
    va = unit['used_va'] + device['va_each']
    outlet_number = len(unit['outlets']) + 1
    reasons = []

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
    if unit['outlet_count'] is None:
        reasons.append('outlet_unknown')
    elif outlet_number > unit['outlet_count']:
        reasons.append('outlet_capacity')

    runtime = None
    curve = unit['runtime_curve']
    if curve is None:
        reasons.append('runtime_unknown')
    else:
        runtime = _interpolate_runtime(curve['points'], watts)
        if runtime is None:
            reasons.append('runtime_unknown')
        elif runtime + 1e-9 < runtime_required:
            reasons.append('runtime_insufficient')

    if reasons:
        return reasons, runtime, None
    safe_w = rating_w / (1 + float(MIN_ENGINEERING_MARGIN))
    safe_va = rating_va / (1 + float(MIN_ENGINEERING_MARGIN))
    utilization = max(watts / safe_w, va / safe_va, outlet_number / unit['outlet_count'])
    return [], runtime, utilization


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
    if reasons & {'outlet_capacity', 'outlet_unknown'}:
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
    model_by_id = {model['product_id']: model for model in models}
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
                reasons, runtime, utilization = _placement(unit, device, parsed['runtime_required'])
                if reasons:
                    current_reasons.append(reasons)
                else:
                    viable.append((utilization, unit['sequence'], unit, runtime))

            if viable:
                _, _, unit, runtime = min(viable, key=lambda item: (-item[0], item[1]))
            elif parsed['mode'] == 'auto' and len(units) < MAX_UPS_UNITS:
                new_options = []
                model_reasons = []
                for model in models:
                    if model['unusable_reasons']:
                        model_reasons.append(model['unusable_reasons'])
                        continue
                    candidate = _new_unit(model, len(units) + 1, auto_added=True)
                    reasons, runtime, utilization = _placement(candidate, device, parsed['runtime_required'])
                    if reasons:
                        model_reasons.append(reasons)
                    else:
                        new_options.append((utilization, model['selection_index'], model, runtime))
                if new_options:
                    _, _, model, runtime = min(new_options, key=lambda item: (-item[0], item[1]))
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

            unit['used_w'] += device['watt_each']
            unit['used_va'] += device['va_each']
            outlet_number = len(unit['outlets']) + 1
            unit['outlets'].append(outlet_number)
            allocation = {
                'deviceIndex': device['device_index'], 'instanceNumber': instance_number,
                'name': device['name'], 'priority': device['priority'], 'quantity': 1,
                'watt': device['watt_each'], 'va': device['va_each'],
                'upsUnitId': unit['ups_unit_id'], 'productId': unit['product_id'],
                'model': unit['model'], 'outletNumber': outlet_number,
            'estimatedRuntimeMinutes': round(runtime, 2), 'runtimeStatus': 'meets_requirement',
            }
            device_allocations.append(allocation)
            outlet_allocations.append({
                'upsUnitId': unit['ups_unit_id'], 'productId': unit['product_id'],
                'outletNumber': outlet_number, 'outletType': None,
                'compatibilityEvidence': 'outlet count only; receptacle type is not recorded',
                'deviceIndex': device['device_index'], 'instanceNumber': instance_number,
                'deviceName': device['name'],
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
        allocation['estimatedRuntimeMinutes'] = round(runtime, 2)

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
    outlet_valid = not bool(reasons & {'outlet_capacity', 'outlet_unknown'})
    runtime_valid = not bool(reasons & {'runtime_insufficient', 'runtime_unknown'})
    all_assigned = not unassigned_devices
    validation = {
        'isValid': all_assigned and capacity_valid and outlet_valid and runtime_valid,
        'allDevicesAssigned': all_assigned,
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
            'autoAdded': unit['auto_added'], 'ratings': {'watts': unit['rating_w'], 'va': unit['rating_va']},
            'usedLoad': {'watts': round(unit['used_w'], 2), 'va': round(unit['used_va'], 2)},
            'utilizationPercent': {
                'watts': None if not unit['rating_w'] else round(unit['used_w'] / unit['rating_w'] * 100, 2),
                'va': None if not unit['rating_va'] else round(unit['used_va'] / unit['rating_va'] * 100, 2),
            },
            'outlets': {'used': len(unit['outlets']), 'available': unit['outlet_count'],
                        'assignedNumbers': list(unit['outlets'])},
            'estimatedRuntimeMinutes': None if runtime is None else round(runtime, 2),
            'runtimeStatus': 'unused' if not unit['used_w'] else 'meets_requirement',
            'runtimeCurveSource': None if not unit['runtime_curve'] else unit['runtime_curve']['source'],
            'technicalEvidence': _technical_evidence(unit),
            'sourceWarnings': list(unit['source_warnings']),
        })

    return {
        'inventoryMode': parsed['mode'],
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
