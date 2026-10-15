"""Explainable UPS recommendation logic over the local SQLite catalog."""

from decimal import Decimal, InvalidOperation
import json
import math
from pathlib import Path
import re
import sqlite3


MIN_ENGINEERING_MARGIN = Decimal('0.20')
TARGET_ENGINEERING_MARGIN = Decimal('0.30')
TOP_N = 5
MAX_DEVICES = 500
RANKING_WEIGHTS = {
    'capacity': 35,
    'runtime': 30,
    'outlet': 15,
    'price': 10,
    'expansion': 10,
}


class RecommendationInputError(ValueError):
    """Request data is not a valid electrical load requirement."""


def _decimal(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None
    return result if result.is_finite() else None


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


def _clean_text(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def _parse_load(request):
    if not isinstance(request, dict):
        raise RecommendationInputError('Request body must be a JSON object')
    power_factor = _decimal(request.get('powerFactor'))
    runtime_required = _decimal(request.get('runtimeRequirementMinutes'))
    if power_factor is None or not Decimal('0') < power_factor <= Decimal('1'):
        raise RecommendationInputError('powerFactor must be greater than 0 and at most 1')
    if _number(power_factor) is None:
        raise RecommendationInputError('powerFactor exceeds the supported numeric range')
    if runtime_required is None or runtime_required <= 0:
        raise RecommendationInputError('runtimeRequirementMinutes must be greater than 0')
    runtime_required_number = _number(runtime_required)
    if runtime_required_number is None:
        raise RecommendationInputError('runtimeRequirementMinutes exceeds the supported numeric range')

    devices = request.get('devices')
    if devices is None and 'deviceName' in request:
        devices = [{
            'name': request.get('deviceName'), 'quantity': request.get('quantity'),
            'watt': request.get('watt'), 'va': request.get('va'),
        }]
    if not isinstance(devices, list) or not devices or len(devices) > MAX_DEVICES:
        raise RecommendationInputError(f'devices must contain 1 to {MAX_DEVICES} entries')

    total_watts = Decimal('0')
    total_va = Decimal('0')
    total_outlets = 0
    warnings = []
    normalized_devices = []
    for index, device in enumerate(devices):
        if not isinstance(device, dict):
            raise RecommendationInputError(f'devices[{index}] must be an object')
        name = _clean_text(device.get('name', device.get('deviceName')))
        quantity_value = device.get('quantity')
        quantity_decimal = _decimal(quantity_value)
        if not name or len(name) > 160:
            raise RecommendationInputError(f'devices[{index}].name must contain 1 to 160 characters')
        if quantity_decimal is None or quantity_decimal != quantity_decimal.to_integral_value() or not 1 <= quantity_decimal <= 10000:
            raise RecommendationInputError(f'devices[{index}].quantity must be an integer from 1 to 10000')
        quantity = int(quantity_decimal)
        watts = _decimal(device.get('watt', device.get('watts')))
        va = _decimal(device.get('va'))
        if watts is None and va is None:
            raise RecommendationInputError(f'devices[{index}] must include watt or va')
        if watts is not None and watts <= 0:
            raise RecommendationInputError(f'devices[{index}].watt must be greater than 0')
        if va is not None and va <= 0:
            raise RecommendationInputError(f'devices[{index}].va must be greater than 0')
        device_pf = _decimal(device.get('powerFactor', power_factor))
        if device_pf is None or not Decimal('0') < device_pf <= Decimal('1'):
            raise RecommendationInputError(f'devices[{index}].powerFactor must be greater than 0 and at most 1')
        if _number(device_pf) is None:
            raise RecommendationInputError(f'devices[{index}].powerFactor exceeds the supported numeric range')
        if watts is None:
            watts = va * device_pf
        if va is None:
            va = watts / device_pf
        watts_number = _number(watts)
        va_number = _number(va)
        if watts_number is None or va_number is None:
            raise RecommendationInputError(f'devices[{index}] load exceeds the supported numeric range')
        if watts > va * Decimal('1.05'):
            warnings.append(f'{name}: watt exceeds VA by more than 5%; both explicit ratings were retained')
        elif abs(watts - va * device_pf) / max(watts, va * device_pf) > Decimal('0.10'):
            warnings.append(f'{name}: W, VA, and power factor do not closely align; both explicit ratings were retained')
        total_watts += watts * quantity
        total_va += va * quantity
        total_outlets += quantity
        normalized_devices.append({
            'name': name, 'quantity': quantity,
            'wattEach': watts_number, 'vaEach': va_number,
            'powerFactor': _number(device_pf),
        })

    effective_pf = total_watts / total_va if total_va else power_factor
    total_watts_number = _number(total_watts)
    total_va_number = _number(total_va)
    effective_pf_number = _number(effective_pf)
    if total_watts_number is None or total_va_number is None or effective_pf_number is None:
        raise RecommendationInputError('total load exceeds the supported numeric range')
    return {
        'devices': normalized_devices,
        'watts': total_watts_number,
        'va': total_va_number,
        'effectivePowerFactor': effective_pf_number,
        'powerFactorInput': _number(power_factor),
        'runtimeRequirementMinutes': runtime_required_number,
        'requiredOutlets': total_outlets,
        'warnings': warnings,
    }


def _numeric_field(value):
    number = _decimal(value)
    return number if number is not None and number > 0 and _number(number) is not None else None


def _non_ups_accessory_reason(description, model, sku):
    """Identify strong product-identity evidence that a UPS-typed row is an accessory."""
    text = _clean_text(' '.join(str(value or '') for value in (description, model, sku))).casefold()
    bundled_ups_pattern = (
        r'\bups\b.*\b\d+(?:\.\d+)?\s*(?:kva|va|kw|w)\b'
        r'.*\bwith\b.{0,40}\b(?:external\s+)?battery\s+pack\b'
    )
    if re.search(bundled_ups_pattern, text):
        return None
    accessory_patterns = (
        r'\b(?:external|replacement|extended)\s+battery\s+(?:pack|module|cabinet|cartridge)\b',
        r'\bbattery\s+(?:pack|module|cabinet|cartridge)\s+(?:for|compatible with|used with)\b',
        r'\breplacement\s+batter(?:y|ies)\b',
    )
    if any(re.search(pattern, text) for pattern in accessory_patterns):
        return 'battery accessory'
    return None


def _parse_runtime_minutes(value):
    match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*(?:min|mins|minute|minutes)\s*', str(value or ''), re.I)
    return _numeric_field(match.group(1)) if match else None


def _parse_integer(value):
    match = re.search(r'\d+', str(value or ''))
    return int(match.group()) if match else None


def _parse_outlets(value):
    text = str(value or '')
    counts = [int(match) for match in re.findall(r'\(\s*(\d+)\s*\)', text)]
    if counts:
        return sum(counts)
    if re.fullmatch(r'\s*\d+\s*', text):
        return int(text)
    counts = [int(match) for match in re.findall(r'(\d+)\s*[x×]\s*(?:IEC|NEMA|C\d)', text, re.I)]
    return sum(counts) if counts else None


def _parse_expansion(value):
    text = _clean_text(value)
    normalized = text.casefold()
    if not text:
        return None, None
    if normalized in {'n/a', 'na', 'none', 'no', 'not supported', 'not available'}:
        return False, None
    if normalized in {'-', '—', 'unknown', '?'}:
        return None, None
    return True, text


def _header_facts(source_data, raw_text):
    headers = source_data.get('source_headers')
    values = str(raw_text or '').split('|')
    facts = {}
    if not isinstance(headers, list) or len(headers) != len(values):
        return facts
    for header, value in zip(headers, values):
        label = _clean_text(header).casefold()
        cell = _clean_text(value)
        if label in {'va', 'output va', 'rating va'}:
            facts['rating_va'] = _numeric_field(cell)
        elif label in {'w', 'watt', 'watts', 'output watts', 'rating watts'}:
            facts['rating_w'] = _numeric_field(cell)
        elif 'capacity' in label and 'va' in label and ('watt' in label or re.search(r'\bva\s*/\s*w', label)):
            pair = re.search(r'(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)', cell)
            if pair:
                facts['rating_va'] = _numeric_field(pair.group(1))
                facts['rating_w'] = _numeric_field(pair.group(2))
        if label in {'pf', 'power factor'}:
            facts['ups_power_factor'] = _numeric_field(cell)
        if 'outlet' in label or 'output connection' in label or 'receptacle' in label or 'socket' in label:
            facts['outlet_count'] = _parse_outlets(cell)
        if label == 'full load' or ('runtime' in label and 'full' in label):
            facts['full_runtime'] = _parse_runtime_minutes(cell)
        if label == 'half load' or ('runtime' in label and 'half' in label):
            facts['half_runtime'] = _parse_runtime_minutes(cell)
        if 'external battery' in label or label in {'ebm', 'external battery module'}:
            facts['external_battery'], facts['external_battery_model'] = _parse_expansion(cell)
        if 'maximum pack of battery' in label or 'max ebm' in label:
            facts['max_external_battery_packs'] = _parse_integer(cell)
    return facts


def _known_catalog_table_facts(manufacturer, relative_path, locator, raw_text):
    """Read exact Full/Half load fields in the supported local source-sheet layouts."""
    if not locator.startswith('sheet:How to Select UPS;row:'):
        return {}
    parts = [_clean_text(part) for part in str(raw_text or '').split('|')]
    path = str(relative_path or '')
    facts = {}
    if manufacturer == 'APC' and 'APC_Commercial_Dealer_20250117.xlsx' in path and len(parts) == 19:
        rating_va_index, rating_w_index = 3, 4
        full_index, half_index = 7, 8
        external_index, max_index = 9, 10
    elif manufacturer == 'Eaton' and 'Eaton_UPS_Dealer_20260101.xlsx' in path and len(parts) == 21:
        rating_va_index, rating_w_index = 3, 4
        full_index, half_index = 9, 10
        outlet_index, external_index, max_index = 8, 11, 12
    elif manufacturer == 'Eaton' and 'EatonUPS_Commercial_Dealer_20250102.xlsx' in path and len(parts) == 21:
        rating_va_index, rating_w_index = 3, 4
        full_index, half_index = 9, 10
        outlet_index, external_index, max_index = 8, 11, 12
    elif manufacturer == 'Eaton' and 'EatonUPS_Commercial_Dealer_20250102.xlsx' in path and len(parts) == 19:
        rating_va_index, rating_w_index = 3, 4
        full_index, half_index = 7, 8
    else:
        return facts

    facts['rating_va'] = _numeric_field(parts[rating_va_index])
    facts['rating_w'] = _numeric_field(parts[rating_w_index])
    if len(parts) > 5:
        facts['ups_power_factor'] = _numeric_field(parts[5])
    facts['full_runtime'] = _parse_runtime_minutes(parts[full_index])
    facts['half_runtime'] = _parse_runtime_minutes(parts[half_index])
    if 'outlet_index' in locals():
        facts['outlet_count'] = _parse_outlets(parts[outlet_index])
    if 'external_index' in locals():
        facts['external_battery'], facts['external_battery_model'] = _parse_expansion(parts[external_index])
    if 'max_index' in locals():
        facts['max_external_battery_packs'] = _parse_integer(parts[max_index])
    return facts


def _model_capacity(text):
    source = str(text or '')
    va_values = []
    watt_values = []
    for value, unit in re.findall(r'(?<![\w.])(\d+(?:\.\d+)?)\s*(kva|va)\b', source, re.I):
        amount = _numeric_field(value)
        if amount is not None:
            va_values.append(amount * (1000 if unit.casefold() == 'kva' else 1))
    for value, unit in re.findall(r'(?<![\w.])(\d+(?:\.\d+)?)\s*(kw|w)(?:att)?\b', source, re.I):
        amount = _numeric_field(value)
        if amount is not None:
            watt_values.append(amount * (1000 if unit.casefold() == 'kw' else 1))
    return va_values, watt_values


def _score_capacity(margin):
    return max(0.0, min(100.0, 100.0 - abs(margin - float(TARGET_ENGINEERING_MARGIN)) * 100.0))


def _interpolate_runtime(points, watts):
    if not points or watts is None:
        return None
    ordered = sorted(points, key=lambda point: point['loadW'])
    load = float(watts)
    if load < ordered[0]['loadW'] or load > ordered[-1]['loadW']:
        return None
    for point in ordered:
        if abs(point['loadW'] - load) < 1e-9:
            return point['runtimeMinutes']
    for lower, upper in zip(ordered, ordered[1:]):
        if lower['loadW'] < load < upper['loadW']:
            fraction = (load - lower['loadW']) / (upper['loadW'] - lower['loadW'])
            return lower['runtimeMinutes'] + fraction * (upper['runtimeMinutes'] - lower['runtimeMinutes'])
    return None


def _source_curve(facts, source_w, source):
    full = facts.get('full_runtime')
    half = facts.get('half_runtime')
    rated_w = _number(source_w)
    if full is None or half is None or rated_w is None or rated_w <= 0 or half < full:
        return None
    return {
        'points': [
            {'loadW': round(rated_w / 2.0, 6), 'runtimeMinutes': float(half)},
            {'loadW': rated_w, 'runtimeMinutes': float(full)},
        ],
        'source': source,
        'batteryConfiguration': 'source-listed; external battery quantity not specified with runtime points',
    }


def _cost_kind(price_type):
    label = _clean_text(price_type).casefold()
    if 'enduser' in label or 'srp' in label or 'list price' in label:
        return None
    if 'total' in label or 'bundle' in label:
        return None
    if 'ndp' in label and 'pack' not in label:
        return 'NDP'
    if 'dealer' in label and 'pack' not in label:
        return 'Dealer'
    return None


def _is_ups_cost_source(relative_path, locator, raw_text):
    context = ' '.join((str(relative_path or ''), str(locator or ''), str(raw_text or ''))).casefold()
    service_markers = (
        'warranty upgrade', 'extended warranty', 'warranty extension',
        'add one year warranty', 'เพิ่มการรับประกัน', 'sheet:service',
    )
    return not any(marker in context for marker in service_markers)


def _build_inventory(connection):
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='published_models'").fetchone():
        try:
            from .published_catalog import load_published_inventory
        except ImportError:
            from published_catalog import load_published_inventory
        return load_published_inventory(connection)
    products = {}
    query = '''SELECT p.id,p.manufacturer_id,p.model,p.sku,p.description,p.product_type,m.name AS manufacturer,
        ps.id AS source_id,ps.raw_json,ps.raw_text,ps.source_locator,sf.relative_path,
        spec.rating_va,spec.rating_w,spec.topology,spec.form_factor,
        po.amount_decimal,po.currency,po.vat_treatment,po.source_date
        FROM products p LEFT JOIN manufacturers m ON m.id=p.manufacturer_id
        LEFT JOIN product_sources ps ON ps.product_id=p.id
        LEFT JOIN source_files sf ON sf.id=ps.source_file_id
        LEFT JOIN product_specifications spec ON spec.product_source_id=ps.id
        LEFT JOIN price_offers po ON po.product_source_id=ps.id
        WHERE p.product_type='UPS' ORDER BY p.id,ps.id'''
    for row in connection.execute(query):
        product = products.setdefault(row['id'], {
            'id': row['id'], 'manufacturer_id': row['manufacturer_id'],
            'manufacturer': row['manufacturer'], 'model': row['model'],
            'sku': row['sku'], 'description': row['description'], 'product_type': row['product_type'],
            'non_ups_reason': _non_ups_accessory_reason(row['description'], row['model'], row['sku']),
            'rating_va_values': set(), 'rating_w_values': set(), 'outlet_values': set(),
            'expansion_values': set(), 'expansion_models': set(), 'max_pack_values': set(),
            'curves': {}, 'prices': [], 'evidence': [], 'warnings': [],
        })
        for field, key in (('rating_va', 'rating_va_values'), ('rating_w', 'rating_w_values')):
            value = _numeric_field(row[field])
            if value is not None:
                product[key].add(value)
        has_rating_va = row['rating_va'] is not None
        has_rating_w = row['rating_w'] is not None

        raw = {}
        if row['raw_json']:
            try:
                raw = json.loads(row['raw_json'])
            except (TypeError, json.JSONDecodeError):
                raw = {}
        if not isinstance(raw, dict):
            raw = {}
        for field, key in (('rating_va', 'rating_va_values'), ('rating_w', 'rating_w_values')):
            value = _numeric_field(raw.get(field))
            if value is not None:
                product[key].add(value)
                if field == 'rating_va':
                    has_rating_va = True
                else:
                    has_rating_w = True
        parsed_va, parsed_w = _model_capacity(product['model'])
        parsed_desc_va, parsed_desc_w = _model_capacity(product['description'])
        product['rating_va_values'].update(parsed_va + parsed_desc_va)
        product['rating_w_values'].update(parsed_w + parsed_desc_w)

        facts = _header_facts(raw, row['raw_text'])
        known_table = _known_catalog_table_facts(
            row['manufacturer'], row['relative_path'], row['source_locator'], row['raw_text'])
        facts.update({key: value for key, value in known_table.items() if value is not None})
        for field, key in (('rating_va', 'rating_va_values'), ('rating_w', 'rating_w_values')):
            value = _numeric_field(facts.get(field))
            if value is not None:
                product[key].add(value)
                if field == 'rating_va':
                    has_rating_va = True
                else:
                    has_rating_w = True
        if facts.get('outlet_count') is not None:
            product['outlet_values'].add(int(facts['outlet_count']))
        if facts.get('external_battery') is not None:
            product['expansion_values'].add(bool(facts['external_battery']))
        if facts.get('external_battery_model'):
            product['expansion_models'].add(str(facts['external_battery_model']))
        if facts.get('max_external_battery_packs') is not None:
            product['max_pack_values'].add(int(facts['max_external_battery_packs']))

        source_ref = {'path': row['relative_path'], 'locator': row['source_locator']}
        curve = _source_curve(facts, facts.get('rating_w') or row['rating_w'] or raw.get('rating_w'), source_ref)
        if curve:
            signature = tuple((point['loadW'], point['runtimeMinutes']) for point in curve['points'])
            product['curves'][signature] = curve
        if row['source_id']:
            product['evidence'].append({
                'path': row['relative_path'], 'locator': row['source_locator'],
                'ratingVaEvidence': has_rating_va, 'ratingWEvidence': has_rating_w,
                'runtimeEvidence': bool(curve), 'outletEvidence': facts.get('outlet_count') is not None,
                'expansionEvidence': facts.get('external_battery') is not None,
            })
        if row['amount_decimal'] is not None:
            price_type = raw.get('price_type')
            kind = _cost_kind(price_type)
            amount = _decimal(row['amount_decimal'])
            if kind and amount is not None and amount > 0 and _is_ups_cost_source(
                    row['relative_path'], row['source_locator'], row['raw_text']):
                product['prices'].append({
                    'amount': row['amount_decimal'], 'decimal': amount, 'currency': row['currency'],
                    'vatTreatment': row['vat_treatment'], 'sourceDate': row['source_date'],
                    'priceEffectiveFrom': raw.get('price_effective_from'),
                    'priceEffectiveTo': raw.get('price_effective_to'),
                    'priceType': _clean_text(price_type), 'kind': kind, 'source': source_ref,
                })

    # Reconcile only an exact manufacturer + source model == explicit manufacturer
    # SKU alias. This keeps model-only and SKU-backed catalogue rows from producing
    # duplicate recommendations without fuzzy matching names.
    sku_matches = {}
    for product in products.values():
        if product['sku'] and not product['non_ups_reason']:
            sku_matches.setdefault((product['manufacturer_id'], product['sku']), []).append(product)
    removed_alias_ids = set()
    for alias in products.values():
        if alias['sku'] or alias['id'] in removed_alias_ids or alias['non_ups_reason']:
            continue
        targets = sku_matches.get((alias['manufacturer_id'], alias['model']), [])
        if len(targets) != 1:
            continue
        target = targets[0]
        if target['id'] == alias['id'] or target['product_type'] != alias['product_type']:
            continue
        for key in ('rating_va_values', 'rating_w_values', 'outlet_values', 'expansion_values',
                    'expansion_models', 'max_pack_values'):
            target[key].update(alias[key])
        target['curves'].update(alias['curves'])
        target['prices'].extend(alias['prices'])
        target['evidence'].extend(alias['evidence'])
        target['warnings'].extend(alias['warnings'])
        target.setdefault('source_product_ids', [target['id']]).append(alias['id'])
        target['warnings'].append('Combined a model-only catalogue row with its exact manufacturer-SKU row')
        removed_alias_ids.add(alias['id'])

    for product in products.values():
        if product['id'] in removed_alias_ids:
            continue
        for field, key in (('rating_va', 'rating_va_values'), ('rating_w', 'rating_w_values')):
            values = product[key]
            product[field] = float(min(values)) if values else None
            if len(values) > 1:
                product['warnings'].append(f'Conflicting source {field} values; used the lowest recorded rating conservatively')
        product['outlet_count'] = min(product['outlet_values']) if product['outlet_values'] else None
        if len(product['outlet_values']) > 1:
            product['warnings'].append('Source records disagree on outlet count; used the lowest recorded count')
        product['external_battery_supported'] = next(iter(product['expansion_values'])) if len(product['expansion_values']) == 1 else None
        if len(product['expansion_values']) > 1:
            product['warnings'].append('Source records disagree on external battery support')
        product['max_external_battery_packs'] = max(product['max_pack_values']) if product['max_pack_values'] else None
        product['external_battery_model'] = sorted(product['expansion_models'])[0] if product['expansion_models'] else None
        if len(product['curves']) == 1:
            product['runtime_curve'] = next(iter(product['curves'].values()))
        else:
            product['runtime_curve'] = None
            if len(product['curves']) > 1:
                product['warnings'].append('Conflicting runtime curves; runtime estimate withheld')
        price_candidates = product['prices']
        product['estimated_cost'] = min(price_candidates, key=lambda item: (item['decimal'], item['kind'])) if price_candidates else None
        if product['estimated_cost'] and product['estimated_cost']['currency'] is None:
            product['warnings'].append('Reseller price currency is not recorded; amount is shown without an assumed currency')
        if product['estimated_cost'] and product['estimated_cost']['sourceDate'] is None:
            product['warnings'].append('Source price date is not recorded; estimate may be historical')
    return [product for product in products.values() if product['id'] not in removed_alias_ids]


def _read_curve_catalog(curve_catalog):
    if curve_catalog is None:
        catalog_path = Path(__file__).with_name('manufacturer_runtime_curves.json')
        if not catalog_path.is_file():
            return []
        try:
            curve_catalog = json.loads(catalog_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f'Invalid manufacturer runtime curve catalog: {error}') from error
    if isinstance(curve_catalog, dict):
        curve_catalog = curve_catalog.get('curves')
    if not isinstance(curve_catalog, list):
        raise ValueError('manufacturer runtime curve catalog must be a list or an object with a curves list')
    return curve_catalog


def _apply_manufacturer_curves(products, curve_catalog):
    matches = 0
    for product in products:
        for entry in curve_catalog:
            if not isinstance(entry, dict) or str(entry.get('manufacturer', '')).casefold() != str(product['manufacturer'] or '').casefold():
                continue
            entry_sku = entry.get('sku')
            entry_model = entry.get('model')
            if entry_sku:
                matches_product = entry_sku == product['sku'] or (
                    not product['sku'] and entry_sku == entry_model == product['model'])
            else:
                matches_product = bool(entry_model and entry_model == product['model'])
            if not matches_product:
                continue
            points = entry.get('points')
            if not isinstance(points, list) or len(points) < 2:
                continue
            normalized_points = []
            valid = True
            for point in points:
                if not isinstance(point, dict):
                    valid = False
                    break
                load_w = _numeric_field(point.get('loadW'))
                runtime_minutes = _numeric_field(point.get('runtimeMinutes'))
                if load_w is None or runtime_minutes is None:
                    valid = False
                    break
                normalized_points.append({'loadW': float(load_w), 'runtimeMinutes': float(runtime_minutes)})
            normalized_points.sort(key=lambda point: point['loadW'])
            if not valid or any(normalized_points[index]['runtimeMinutes'] > normalized_points[index - 1]['runtimeMinutes']
                                for index in range(1, len(normalized_points))):
                continue
            source = entry.get('source')
            if not isinstance(source, dict) or not source.get('url'):
                continue
            previous = product['runtime_curve']
            if previous and previous['points'] != normalized_points:
                product['warnings'].append('Official manufacturer runtime table overrides a differing local source curve')
            product['runtime_curve'] = {
                'points': normalized_points,
                'source': source,
                'batteryConfiguration': entry.get('batteryConfiguration') or 'unspecified',
            }
            if entry.get('conditions'):
                product['runtime_curve']['conditions'] = entry['conditions']
            if entry.get('outletCount') is not None:
                outlet_count = entry['outletCount']
                if isinstance(outlet_count, int) and not isinstance(outlet_count, bool) and outlet_count > 0:
                    if product['outlet_count'] is not None and product['outlet_count'] != outlet_count:
                        product['warnings'].append('Official outlet count overrides a differing local source count')
                    product['outlet_count'] = outlet_count
                    product['manufacturer_outlet_source'] = source
            matches += 1
            break
    return matches


def _price_scores(candidates):
    priced = [candidate for candidate in candidates if candidate['estimated_cost']]
    currencies = {candidate['estimated_cost']['currency'] for candidate in priced}
    vat_treatments = {candidate['estimated_cost']['vatTreatment'] for candidate in priced}
    if not priced:
        for candidate in candidates:
            candidate['scores']['price'] = None
        return False, 'No explicit NDP/Dealer prices are available for comparison.'
    if None in currencies or len(currencies) != 1:
        for candidate in candidates:
            candidate['scores']['price'] = None
        return False, 'Price efficiency is not scored because explicit comparable currencies are unavailable.'
    if 'unknown' in vat_treatments or len(vat_treatments) != 1:
        for candidate in candidates:
            candidate['scores']['price'] = None
        return False, 'Price efficiency is not scored because VAT treatments are unknown or differ.'
    cost_per_watt = {
        candidate['id']: candidate['estimated_cost']['decimal'] / Decimal(str(candidate['rating_w']))
        for candidate in priced if candidate['rating_w']
    }
    if not cost_per_watt:
        for candidate in candidates:
            candidate['scores']['price'] = None
        return False, 'Price efficiency is unavailable because capacity ratings are missing.'
    lowest = min(cost_per_watt.values())
    for candidate in candidates:
        ratio = cost_per_watt.get(candidate['id'])
        candidate['scores']['price'] = round(float(lowest / ratio * 100), 2) if ratio else None
    return True, None


def recommend_from_connection(connection, request, limit=TOP_N, curve_catalog=None):
    """Return explainable capacity-fit UPS candidates from an open SQLite connection."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
        raise RecommendationInputError('limit must be an integer from 1 to 20')
    if connection.row_factory is None:
        connection.row_factory = sqlite3.Row
    load = _parse_load(request)
    inventory = _build_inventory(connection)
    _apply_manufacturer_curves(inventory, _read_curve_catalog(curve_catalog))
    candidates = []
    excluded_missing_capacity = 0
    excluded_margin = 0
    excluded_non_ups_accessory = 0
    for product in inventory:
        if product['non_ups_reason']:
            excluded_non_ups_accessory += 1
            continue
        rating_w, rating_va = product['rating_w'], product['rating_va']
        if not rating_w or not rating_va:
            excluded_missing_capacity += 1
            continue
        margin_w = rating_w / load['watts'] - 1
        margin_va = rating_va / load['va'] - 1
        if not math.isfinite(margin_w) or not math.isfinite(margin_va):
            raise RecommendationInputError('load produces a calculation outside the supported numeric range')
        margin_w_percent = margin_w * 100
        margin_va_percent = margin_va * 100
        if not math.isfinite(margin_w_percent) or not math.isfinite(margin_va_percent):
            raise RecommendationInputError('load produces a calculation outside the supported numeric range')
        if margin_w + 1e-9 < float(MIN_ENGINEERING_MARGIN) or margin_va + 1e-9 < float(MIN_ENGINEERING_MARGIN):
            excluded_margin += 1
            continue
        margin = min(margin_w, margin_va)
        curve = product['runtime_curve']
        runtime = _interpolate_runtime(curve['points'], load['watts']) if curve else None
        runtime_requirement = load['runtimeRequirementMinutes']
        runtime_status = 'unknown' if runtime is None else 'meets_runtime' if runtime >= runtime_requirement else 'below_runtime_requirement'
        outlet_count = product['outlet_count']
        outlet_score = None if outlet_count is None else 100.0 if load['requiredOutlets'] == 0 else min(100.0, 100 * outlet_count / load['requiredOutlets'])
        runtime_score = None if runtime is None else min(100.0, 100 * runtime / runtime_requirement)
        expansion_score = None if product['external_battery_supported'] is None else 100.0 if product['external_battery_supported'] else 0.0
        capacity_score = _score_capacity(margin)
        scores = {
            'capacity': round(capacity_score, 2), 'runtime': None if runtime_score is None else round(runtime_score, 2),
            'outlet': None if outlet_score is None else round(outlet_score, 2),
            'price': None,
            'expansion': None if expansion_score is None else round(expansion_score, 2),
        }
        candidate = {
            **product,
            'engineering_margin': {'watts_percent': round(margin_w_percent, 2), 'va_percent': round(margin_va_percent, 2),
                                   'minimum_percent': round(margin * 100, 2)},
            'estimated_runtime_minutes': None if runtime is None else round(runtime, 2),
            'runtime_status': runtime_status,
            'outlet_score': None if outlet_score is None else round(outlet_score, 2),
            'outlet_shortfall': None if outlet_count is None else max(0, load['requiredOutlets'] - outlet_count),
            'scores': scores,
            'runtime_curve': curve,
        }
        candidates.append(candidate)

    comparable_prices, price_score_note = _price_scores(candidates)
    for candidate in candidates:
        available = [(name, score, RANKING_WEIGHTS[name]) for name, score in candidate['scores'].items() if score is not None]
        total_weight = sum(weight for _, _, weight in available)
        candidate['weighted_score'] = round(sum(score * weight for _, score, weight in available) / total_weight, 2) if total_weight else 0
        candidate['evidence_coverage_percent'] = round(total_weight, 2)
        candidate['included_score_factors'] = [name for name, _, _ in available]

    status_order = {'meets_runtime': 0, 'unknown': 1, 'below_runtime_requirement': 2}
    candidates.sort(key=lambda candidate: (
        status_order[candidate['runtime_status']], -candidate['weighted_score'],
        -candidate['evidence_coverage_percent'], candidate['manufacturer'] or '', candidate['model'] or '',
    ))
    recommendations = []
    for rank, candidate in enumerate(candidates[:limit], 1):
        reasons = [
            f"Capacity margin: {candidate['engineering_margin']['watts_percent']:.1f}% W and {candidate['engineering_margin']['va_percent']:.1f}% VA; minimum required {MIN_ENGINEERING_MARGIN * 100:.0f}%.",
        ]
        if candidate['estimated_runtime_minutes'] is None:
            reasons.append('No matching source runtime curve is available; runtime fit is unknown.')
        elif candidate['runtime_status'] == 'meets_runtime':
            reasons.append(f"Source-curve runtime estimate {candidate['estimated_runtime_minutes']:.1f} min meets {load['runtimeRequirementMinutes']:.1f} min requirement.")
        else:
            reasons.append(f"Source-curve runtime estimate {candidate['estimated_runtime_minutes']:.1f} min is below the {load['runtimeRequirementMinutes']:.1f} min requirement.")
        if candidate['outlet_count'] is None:
            reasons.append('Outlet count is not present in the source record.')
        else:
            reasons.append(f"Outlet count {candidate['outlet_count']} for {load['requiredOutlets']} device plugs; no outlet allocation is performed.")
        cost = candidate['estimated_cost']
        if cost:
            reasons.append(f"Lowest explicit NDP/Dealer source amount is {cost['amount']} ({cost['currency'] or 'currency unknown'}; {cost['vatTreatment']} VAT treatment).")
        else:
            reasons.append('No explicit NDP/Dealer price row is available.')
        if candidate['external_battery_supported'] is True:
            reasons.append('Source record identifies external-battery expansion support.')
        elif candidate['external_battery_supported'] is False:
            reasons.append('Source record marks external-battery expansion as unavailable.')
        else:
            reasons.append('External-battery expansion capability is unknown in the source data.')
        def evidence_refs(flag):
            refs = []
            seen = set()
            for evidence in candidate['evidence']:
                if evidence.get(flag) and (evidence.get('path'), evidence.get('locator')) not in seen:
                    seen.add((evidence.get('path'), evidence.get('locator')))
                    refs.append({'path': evidence.get('path'), 'locator': evidence.get('locator')})
                if len(refs) >= 3:
                    break
            return refs
        warnings = list(candidate['warnings'])
        if candidate['runtime_curve'] and candidate['runtime_curve']['source'].get('path') and candidate['external_battery_supported'] is True:
            warnings.append('Source runtime points do not state how many external battery packs, if any, are included')
        if not comparable_prices:
            warnings.append(price_score_note)
        if candidate['outlet_shortfall']:
            warnings.append(f"Outlet shortfall: {candidate['outlet_shortfall']} more outlets needed; extension/allocation not planned.")
        estimated_cost = None
        if cost:
            estimated_cost = {key: value for key, value in cost.items() if key != 'decimal' and key != 'kind'}
            estimated_cost['basis'] = cost['kind']
        outlet_evidence = evidence_refs('outletEvidence')
        if candidate.get('manufacturer_outlet_source'):
            outlet_evidence.append(candidate['manufacturer_outlet_source'])
        recommendations.append({
            'rank': rank,
            'productId': candidate['id'], 'manufacturer': candidate['manufacturer'],
            'model': candidate['model'], 'sku': candidate['sku'],
            'ratings': {'va': candidate['rating_va'], 'watts': candidate['rating_w']},
            'engineeringMargin': {
                'wattsPercent': candidate['engineering_margin']['watts_percent'],
                'vaPercent': candidate['engineering_margin']['va_percent'],
                'minimumPercent': candidate['engineering_margin']['minimum_percent'],
            },
            'estimatedRuntimeMinutes': candidate['estimated_runtime_minutes'],
            'runtimeStatus': candidate['runtime_status'],
            'runtimeCurve': candidate['runtime_curve'],
            'technicalEvidence': {
                'ratingVa': evidence_refs('ratingVaEvidence'),
                'ratingWatts': evidence_refs('ratingWEvidence'),
                'runtimeCurve': ([candidate['runtime_curve']['source']] if candidate['runtime_curve'] else []),
                'outlets': outlet_evidence,
                'expansion': evidence_refs('expansionEvidence'),
            },
            'estimatedResellerCost': estimated_cost,
            'outletFitness': {'required': load['requiredOutlets'], 'available': candidate['outlet_count'],
                              'shortfall': candidate['outlet_shortfall']},
            'expansionCapability': {'supported': candidate['external_battery_supported'],
                                    'batteryModel': candidate['external_battery_model'],
                                    'maximumPacks': candidate['max_external_battery_packs']},
            'scores': candidate['scores'], 'weightedScore': candidate['weighted_score'],
            'evidenceCoveragePercent': candidate['evidence_coverage_percent'],
            'includedScoreFactors': candidate['included_score_factors'],
            'reasons': reasons, 'warnings': warnings,
        })

    return {
        'load': load,
        'rankingFormula': {
            'weightsPercent': dict(RANKING_WEIGHTS),
            'formula': 'weighted mean of available 0-100 factors; missing factors are omitted and exposed through evidenceCoveragePercent',
            'factorFormulas': {
                'capacity': 'clamp(100 - 100 * abs(min(W margin, VA margin) - 0.30), 0, 100); both margins must be at least 0.20',
                'runtime': 'min(100, estimatedRuntimeMinutes / runtimeRequirementMinutes * 100); interpolate between exact source load points only',
                'outlet': 'min(100, sourceOutletCount / requiredDeviceCount * 100); no outlet allocation',
                'price': 'min(resellerAmount / ratedWatts) / candidate(resellerAmount / ratedWatts) * 100; only with one known comparable currency',
                'expansion': '100 if an external battery is explicitly supported, 0 if explicitly unsupported, otherwise unknown',
            },
            'sortOrder': ['runtime-meets', 'runtime-unknown', 'runtime-below-target', 'weightedScore descending', 'evidenceCoverage descending'],
            'minimumEngineeringMarginPercent': float(MIN_ENGINEERING_MARGIN * 100),
            'targetEngineeringMarginPercent': float(TARGET_ENGINEERING_MARGIN * 100),
            'runtimeInterpolation': 'linear between source Full load and Half load points only; no extrapolation',
        },
        'eligibleProductCount': len(candidates),
        'excludedProductCount': {
            'missingCapacity': excluded_missing_capacity,
            'belowMinimumMargin': excluded_margin,
            'nonUpsAccessory': excluded_non_ups_accessory,
        },
        'recommendations': recommendations,
        'recommendedUps': recommendations[0] if recommendations else None,
        'alternativeUps': recommendations[1:],
        'warnings': load['warnings'] + ([price_score_note] if not comparable_prices and price_score_note else []),
    }


def recommend(database_path, request, limit=TOP_N, curve_catalog=None):
    path = Path(database_path)
    if not path.is_file():
        raise FileNotFoundError(f'UPS database not found: {path}')
    connection = sqlite3.connect(str(path), timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only = ON')
    try:
        return recommend_from_connection(connection, request, limit, curve_catalog)
    finally:
        connection.close()
