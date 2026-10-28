"""Overlay exact-model manufacturer facts without changing source inventories.

Records are curated facts, not evidence inferred from SKU prefixes or product
names. Prices and legacy source observations are preserved in the deep copy.
"""

from copy import deepcopy
from decimal import Decimal
from datetime import date
import hashlib
import json
import math
import re
from urllib.parse import urlsplit


MANUFACTURER_DOMAINS = {
    'apc': ('se.com', 'apc.com', 'schneider-electric.com'),
    'schneider electric': ('se.com', 'schneider-electric.com'),
    'eaton': ('eaton.com',),
    'cyberpower': ('cyberpower.com', 'cyberpowersystems.com'),
    'delta': ('deltaww.com', 'deltapowersolutions.com'),
    'syndome': ('syndome.com', 'syndome.co.th'),
    'vertiv': ('vertiv.com',),
}
_CATEGORIES = {'UPS', 'voltage_regulator', 'inverter'}
_OUTLET_TYPES = {None, 'IEC_C13', 'IEC_C19', 'NEMA_5_15R', 'UNIVERSAL', 'AS', 'HARDWIRE'}
_OUTLET_POWERS = {'battery', 'surge', 'unknown'}


def _text(value, limit=300):
    return (isinstance(value, str) and 0 < len(value) <= limit
            and value == value.strip() and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def _source(value, manufacturer):
    """Return only a vendor HTTPS URL; reject userinfo and URL decorations."""
    if not isinstance(value, dict):
        return None
    url = value.get('url')
    if (not _text(url, 4096) or any(char.isspace() for char in url)
            or '?' in url or '#' in url):
        return None
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        parsed.port  # Reject malformed ports, even though no URL is fetched here.
        if (parsed.scheme != 'https' or not host or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment):
            return None
        if (len(host) > 253 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                                   for label in host.split('.'))):
            return None
        domains = MANUFACTURER_DOMAINS.get(manufacturer.casefold(), ())
        if not any(host == domain or host.endswith('.' + domain) for domain in domains):
            return None
    except (TypeError, ValueError):
        return None
    result = {'url': url}
    page = value.get('page')
    if isinstance(page, int) and not isinstance(page, bool) and page > 0:
        result['page'] = page
    return result


def is_manufacturer_url(manufacturer, url):
    """Check source authority for one manufacturer without fetching the URL."""
    return isinstance(manufacturer, str) and bool(_source({'url': url}, manufacturer))


def has_unverified_bundle(product):
    """Legacy model-only alias merges do not establish identical bundles."""
    if not isinstance(product, dict):
        return True
    if product.get('bundle_identity_unverified') is True:
        return True
    warnings = product.get('warnings') if isinstance(product.get('warnings'), list) else []
    return any(isinstance(warning, str) and 'combined a model-only catalogue row' in warning.casefold()
               for warning in warnings)


def _crosswalk(value, product, manufacturer):
    """Accept a distributor mapping only when it is tied to this product's evidence."""
    if not isinstance(value, dict):
        return None
    source_id = value.get('sourceId')
    manufacturer_url = value.get('manufacturerUrl')
    if (not isinstance(source_id, str) or not re.fullmatch(r'[0-9a-fA-F]{24}', source_id)
            or not is_manufacturer_url(manufacturer, manufacturer_url)):
        return None
    evidence = product.get('evidence')
    if not isinstance(evidence, list):
        return None
    source_id = source_id.lower()
    for item in evidence:
        if not isinstance(item, dict):
            continue
        if 'sourceId' in item:
            existing = item.get('sourceId')
            if isinstance(existing, str) and re.fullmatch(r'[0-9a-fA-F]{24}', existing):
                if existing.lower() == source_id:
                    return {'sourceId': source_id, 'manufacturerUrl': manufacturer_url}
            continue
        if not _text(item.get('path'), 4096) or not _text(item.get('locator'), 1000):
            continue
        try:
            serialized = json.dumps(item, sort_keys=True, ensure_ascii=False)
            candidate = hashlib.sha256(serialized.encode('utf-8')).hexdigest()[:24]
        except (TypeError, ValueError, UnicodeEncodeError):
            continue
        if candidate == source_id:
            return {'sourceId': source_id, 'manufacturerUrl': manufacturer_url}
    return None


def _positive_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    try:
        numeric = float(value)
    except (OverflowError, ValueError):
        return None
    return numeric if math.isfinite(numeric) and numeric > 0 else None


def _key(record):
    if not isinstance(record, dict) or 'catalogSku' not in record:
        return None
    manufacturer = record.get('manufacturer')
    maker_sku = record.get('manufacturerSku')
    catalog_sku = record.get('catalogSku')
    category = record.get('category')
    if (not _text(manufacturer) or manufacturer.casefold() not in MANUFACTURER_DOMAINS
            or not _text(maker_sku) or not isinstance(category, str) or category not in _CATEGORIES
            or not _source(record.get('identitySource'), manufacturer)):
        return None
    if catalog_sku is None:
        return manufacturer.casefold(), 'model', maker_sku
    if not _text(catalog_sku):
        return None
    return manufacturer.casefold(), 'sku', catalog_sku


def _product_key(product):
    if not isinstance(product, dict) or not _text(product.get('manufacturer')):
        return None
    manufacturer = product['manufacturer'].casefold()
    sku = product.get('sku')
    if sku is None or sku == '':
        model = product.get('model')
        return (manufacturer, 'model', model) if _text(model) else None
    return (manufacturer, 'sku', sku) if _text(sku) else None


def _field(value):
    normalized = re.sub(r'[^a-z0-9]+', '', value.casefold()) if isinstance(value, str) else ''
    if normalized in {'identity', 'sku', 'model', 'manufacturer', 'manufacturersku',
                      'catalogsku', 'category', 'productcategory', 'modelidentity'}:
        return 'identity'
    if normalized in {'ratingwatts', 'ratingw', 'watts', 'w', 'outputw'}:
        return 'rating_w'
    if normalized in {'ratingva', 'va', 'outputva'}:
        return 'rating_va'
    if normalized in {'capacity', 'ratings', 'rating'}:
        return 'ratings'
    if normalized.startswith(('outlet', 'batterybackup', 'batterybacked')):
        return 'outlets'
    if normalized.startswith(('runtime', 'batteryconfiguration')):
        return 'runtime'
    return 'unknown'


def _conflicts(record):
    value = record.get('conflicts', [])
    return value if isinstance(value, list) else [{'field': 'unknown'}]


def _safe_conflict(conflict, manufacturer):
    if not isinstance(conflict, dict):
        return {'field': 'unknown'}
    result = {'field': conflict.get('field') if _text(conflict.get('field')) else 'unknown'}
    for key in ('configurationId', 'outletType'):
        if _text(conflict.get(key)):
            result[key] = conflict[key]
    for key in ('sourceA', 'sourceB'):
        value = conflict.get(key)
        safe = _source({'url': value}, manufacturer) if isinstance(value, str) else _source(value, manufacturer)
        if safe:
            result[key] = safe
    for key in ('valueA', 'valueB'):
        value = conflict.get(key)
        if _text(value, 300):
            result[key] = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                if math.isfinite(float(value)):
                    result[key] = value
            except (OverflowError, ValueError):
                pass
    return result


def _remember_conflicts(product, conflicts, manufacturer):
    if not conflicts:
        return
    existing = product.get('manufacturer_conflicts')
    if not isinstance(existing, list):
        existing = []
        product['manufacturer_conflicts'] = existing
    for conflict in conflicts:
        safe = _safe_conflict(conflict, manufacturer)
        if safe not in existing:
            existing.append(safe)


def _warning(product, message):
    warnings = product.get('warnings')
    if not isinstance(warnings, list):
        warnings = []
        product['warnings'] = warnings
    if message not in warnings:
        warnings.append(message)


def _history(product, field, values):
    if not values:
        return
    key = 'previous_' + field
    previous = product.get(key)
    if not isinstance(previous, list):
        previous = []
        product[key] = previous
    for value in values:
        if value not in previous:
            previous.append(deepcopy(value))


def _packs(value):
    if not isinstance(value, list):
        return None
    packs = []
    for item in value:
        if not isinstance(item, dict) or not _text(item.get('sku')):
            return None
        quantity = item.get('quantity')
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 0 < quantity <= 10000:
            return None
        packs.append({'sku': item['sku'], 'quantity': quantity})
    return packs


def _lifecycle_observation(value, manufacturer):
    """Preserve a regional manufacturer notice without inferring Thai status."""
    if not isinstance(value, dict) or not _text(value.get('country'), 80):
        return None
    country = value['country']
    if not re.fullmatch(r'[A-Za-z][A-Za-z .-]{0,79}', country):
        return None
    source = _source(value.get('source'), manufacturer)
    dates = {}
    for key in ('endOfSaleDate', 'observedOn'):
        raw = value.get(key)
        if not isinstance(raw, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw):
            return None
        try:
            dates[key] = date.fromisoformat(raw).isoformat()
        except ValueError:
            return None
    if not source:
        return None
    return {'country': country, **dates, 'thailandStatus': 'unknown', 'source': source}


def _configuration(value, manufacturer, rated_w):
    if (not isinstance(value, dict) or not isinstance(value.get('id'), str)
            or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}', value['id'])
            or not _text(value.get('label'))):
        return None
    source = _source(value.get('source'), manufacturer)
    included, extra = _packs(value.get('includedPacks')), _packs(value.get('extraPacks'))
    points = value.get('points')
    conditions = value.get('conditions')
    if (not source or included is None or extra is None or not isinstance(points, list)
            or len(points) < 2 or conditions is not None and not _text(conditions, 1000)):
        return None
    normalized = []
    for point in points:
        if not isinstance(point, dict):
            return None
        load, runtime = _positive_number(point.get('loadW')), _positive_number(point.get('runtimeMinutes'))
        if load is None or runtime is None or rated_w is not None and load > rated_w:
            return None
        normalized.append({'loadW': load, 'runtimeMinutes': runtime})
    normalized.sort(key=lambda point: point['loadW'])
    if any(normalized[i]['loadW'] <= normalized[i - 1]['loadW']
           or normalized[i]['runtimeMinutes'] > normalized[i - 1]['runtimeMinutes']
           for i in range(1, len(normalized))):
        return None
    return {'id': value['id'], 'label': value['label'], 'points': normalized, 'source': source,
            'includedPacks': included, 'extraPacks': extra, 'conditions': conditions,
            'manufacturer_verified': True}


def _group(value, manufacturer):
    if not isinstance(value, dict):
        return None
    group_type, power, count = value.get('type'), value.get('power'), value.get('count')
    try:
        valid_type = group_type in _OUTLET_TYPES
    except TypeError:
        return None
    source = _source(value.get('source'), manufacturer)
    if (not valid_type or not isinstance(power, str) or power not in _OUTLET_POWERS
            or not source or isinstance(count, bool)
            or not isinstance(count, int) or not 0 <= count <= 10000):
        return None
    return {'type': group_type, 'power': power, 'count': count, 'source': source,
            'manufacturer_verified': True}


def _facts_match(left, right, ignored=('source',)):
    return ({key: value for key, value in left.items() if key not in ignored}
            == {key: value for key, value in right.items() if key not in ignored})


def _merge(product, field, incoming, key_of, conflict_field):
    current = product.get(field)
    if not isinstance(current, list):
        current = []
    else:
        current = deepcopy(current)
    blocked = set()
    for new in incoming:
        key = key_of(new)
        if key in blocked:
            _history(product, field, [new])
            continue
        prior = [value for value in current if isinstance(value, dict) and key_of(value) == key]
        def valid_prior(value):
            if value.get('manufacturer_verified') is not True:
                return False
            if field == 'outlet_groups':
                return _group(value, product['manufacturer']) is not None
            rated = _positive_number(product.get('rating_w')) if product.get('rating_w_verified') is True else None
            return _configuration(value, product['manufacturer'], rated) is not None
        legacy = [value for value in prior if not valid_prior(value)]
        if legacy:
            _history(product, field, legacy)
            current = [value for value in current if value not in legacy]
            prior = [value for value in prior if value not in legacy]
        if not prior:
            current.append(new)
        elif not all(_facts_match(value, new) for value in prior):
            _history(product, field, prior)
            _history(product, field, [new])
            current = [value for value in current if not isinstance(value, dict) or key_of(value) != key]
            blocked.add(key)
            conflict = {'field': conflict_field}
            if conflict_field == 'runtime' and _text(key):
                conflict['configurationId'] = key
            elif conflict_field == 'outlets' and _text(key[0]):
                conflict['outletType'] = key[0]
            _remember_conflicts(product, [conflict], product['manufacturer'])
            _warning(product, 'Conflicting manufacturer evidence requires review')
    product[field] = current


def _invalidate_list(product, field, selector=None):
    values = product.get(field)
    if not isinstance(values, list):
        product[field] = []
        return
    removed = [value for value in values if selector is None or selector(value)]
    _history(product, field, removed)
    product[field] = [value for value in values if value not in removed]


def _invalidate_prior_conflicts(product):
    """Keep unresolved conflicts fail-closed even when a new batch omits the SKU."""
    conflicts = product.get('manufacturer_conflicts')
    if conflicts is None or conflicts == []:
        return
    if not isinstance(conflicts, list):
        conflicts = [{'field': 'unknown'}]
    parsed = [(item, _field(item.get('field')) if isinstance(item, dict) else 'unknown')
              for item in conflicts]
    fields = {kind for _, kind in parsed}
    _warning(product, 'Unresolved manufacturer evidence conflict requires review')
    if 'identity' in fields or 'unknown' in fields:
        product['identity_verified'] = False
        product['rating_w_verified'] = False
        product['rating_va_verified'] = False
        _invalidate_list(product, 'outlet_groups')
        _invalidate_list(product, 'battery_configurations')
        return
    if 'rating_w' in fields or 'ratings' in fields:
        product['rating_w_verified'] = False
    if 'rating_va' in fields or 'ratings' in fields:
        product['rating_va_verified'] = False
    outlets = [item for item, kind in parsed if kind == 'outlets']
    if any(not isinstance(item, dict) or not _text(item.get('outletType')) for item in outlets):
        _invalidate_list(product, 'outlet_groups')
    elif outlets:
        types = {item['outletType'] for item in outlets}
        _invalidate_list(product, 'outlet_groups', lambda item:
                         isinstance(item, dict) and item.get('type') in types)
    runtime = [item for item, kind in parsed if kind == 'runtime']
    if any(not isinstance(item, dict) or not _text(item.get('configurationId')) for item in runtime):
        _invalidate_list(product, 'battery_configurations')
    elif runtime:
        ids = {item['configurationId'] for item in runtime}
        _invalidate_list(product, 'battery_configurations', lambda item:
                         isinstance(item, dict) and item.get('id') in ids)


def apply_verified_evidence(inventory, records):
    """Return a deep-copied inventory enriched by exact, trusted OEM records.

    Duplicate identity records with differing content reject the whole batch.
    A missing source or a value that cannot be verified never upgrades a fact.
    Existing verified fields are retained across partial refreshes; explicit
    unresolved conflicts invalidate only the applicable fields/configurations.
    """
    if not isinstance(inventory, list) or not isinstance(records, list):
        raise TypeError('inventory and records must be lists')
    index = {}
    for record in records:
        key = _key(record)
        if key is None:
            continue
        if key in index and index[key] != record:
            raise ValueError('Conflicting duplicate evidence records')
        index[key] = record

    result = deepcopy(inventory)
    for product in result:
        if not isinstance(product, dict):
            continue
        if has_unverified_bundle(product):
            product['bundle_identity_unverified'] = True
        product.setdefault('identity_verified', False)
        product.setdefault('rating_w_verified', False)
        product.setdefault('rating_va_verified', False)
        manufacturer = product.get('manufacturer')
        for flag, source_field in (('identity_verified', 'identity_source'),
                                   ('rating_w_verified', 'rating_w_source'),
                                   ('rating_va_verified', 'rating_va_source')):
            if product.get(flag) is not True:
                product[flag] = False
                continue
            valid = isinstance(manufacturer, str) and bool(_source(product.get(source_field), manufacturer))
            if flag == 'identity_verified':
                maker_sku = product.get('manufacturer_sku')
                valid = valid and _text(maker_sku)
                catalog_sku = product.get('sku')
                if catalog_sku is None or catalog_sku == '':
                    valid = valid and product.get('model') == maker_sku
                elif catalog_sku != maker_sku:
                    valid = valid and bool(_crosswalk(product.get('identity_crosswalk'), product, manufacturer))
            else:
                target = flag.removesuffix('_verified')
                valid = valid and _positive_number(product.get(target)) is not None
            if not valid:
                product[flag] = False
        _invalidate_prior_conflicts(product)
        key = _product_key(product)
        record = index.get(key)
        if record is None:
            continue
        manufacturer = product['manufacturer']
        crosswalk = None
        if record['catalogSku'] is not None and record['catalogSku'] != record['manufacturerSku']:
            crosswalk = _crosswalk(record.get('crosswalkSource'), product, manufacturer)
            if crosswalk is None:
                continue
        conflicts = _conflicts(record)
        prior_conflicts = product.get('manufacturer_conflicts')
        all_conflicts = (prior_conflicts if isinstance(prior_conflicts, list) else []) + conflicts
        parsed = [(item, _field(item.get('field')) if isinstance(item, dict) else 'unknown')
                  for item in all_conflicts]
        _remember_conflicts(product, conflicts, manufacturer)
        if parsed:
            _warning(product, 'Unresolved manufacturer evidence conflict requires review')
        fields = {kind for _, kind in parsed}
        if 'identity' in fields or 'unknown' in fields:
            product['identity_verified'] = False
            product['rating_w_verified'] = False
            product['rating_va_verified'] = False
            _invalidate_list(product, 'outlet_groups')
            _invalidate_list(product, 'battery_configurations')
            continue

        prior_sku = product.get('manufacturer_sku')
        if (product.get('identity_verified') is True and _text(prior_sku)
                and prior_sku != record['manufacturerSku']):
            _remember_conflicts(product, [{'field': 'identity', 'valueA': prior_sku,
                                           'valueB': record['manufacturerSku'],
                                           'sourceA': product.get('identity_source'),
                                           'sourceB': record.get('identitySource')}], manufacturer)
            _warning(product, 'Conflicting manufacturer SKU crosswalk requires review')
            product['identity_verified'] = False
            product['rating_w_verified'] = False
            product['rating_va_verified'] = False
            _invalidate_list(product, 'outlet_groups')
            _invalidate_list(product, 'battery_configurations')
            continue
        product['identity_verified'] = True
        product['manufacturer_sku'] = record['manufacturerSku']
        if crosswalk is not None:
            previous_crosswalk = product.get('identity_crosswalk')
            if previous_crosswalk and previous_crosswalk != crosswalk:
                _history(product, 'identity_crosswalk', [previous_crosswalk])
            product['identity_crosswalk'] = crosswalk
        identity_source = _source(record['identitySource'], manufacturer)
        if product.get('identity_source') and product['identity_source'] != identity_source:
            _history(product, 'identity_source', [product['identity_source']])
        product['identity_source'] = identity_source
        product['product_category'] = record['category']
        observation = _lifecycle_observation(record.get('lifecycleObservation'), manufacturer)
        if observation:
            observations = product.setdefault('lifecycle_observations', [])
            if isinstance(observations, list) and observation not in observations:
                observations.append(observation)
        if record['category'] != 'UPS':
            if product.get('non_ups_reason') and product['non_ups_reason'] != 'manufacturer_verified_non_ups':
                product.setdefault('previous_non_ups_reason', product['non_ups_reason'])
            product['non_ups_reason'] = 'manufacturer_verified_non_ups'
        elif product.get('non_ups_reason') == 'manufacturer_verified_non_ups':
            product['non_ups_reason'] = None
        elif product.get('non_ups_reason'):
            _warning(product, 'Manufacturer UPS classification conflicts with a legacy non-UPS reason')

        source = _source(record.get('ratingSource'), manufacturer)
        curve_cap = None
        for record_field, target, conflict_name in (
                ('ratingWatts', 'rating_w', 'rating_w'), ('ratingVa', 'rating_va', 'rating_va')):
            if 'ratings' in fields or conflict_name in fields:
                product[target + '_verified'] = False
                continue
            value = _positive_number(record.get(record_field))
            if source and value is not None:
                previous = product.get(target)
                if product.get(target + '_verified') is True and previous is not None and previous != value:
                    _remember_conflicts(product, [{'field': record_field, 'valueA': previous,
                                                   'valueB': value, 'sourceA': product.get(target + '_source'),
                                                   'sourceB': source}], manufacturer)
                    _warning(product, 'Conflicting manufacturer ratings require review')
                    product[target + '_verified'] = False
                    if target == 'rating_w':
                        old_value = _positive_number(previous)
                        curve_cap = min(old_value, value) if old_value is not None else value
                    continue
                if previous is not None and previous != value:
                    history = product.setdefault('original_source_ratings', {})
                    if isinstance(history, dict):
                        history.setdefault(target, deepcopy(previous))
                    _warning(product, 'Manufacturer rating differs from a prior source; prior rating retained')
                previous_source = product.get(target + '_source')
                if previous_source and previous_source != source:
                    _history(product, target + '_source', [previous_source])
                product[target] = value
                product[target + '_verified'] = True
                product[target + '_source'] = deepcopy(source)
                if target == 'rating_w':
                    curve_cap = value

        overrated_configs = set()
        if curve_cap is not None and isinstance(product.get('battery_configurations'), list):
            for config in product['battery_configurations']:
                if not isinstance(config, dict) or not _text(config.get('id')):
                    continue
                points = config.get('points')
                if isinstance(points, list) and any(
                        isinstance(point, dict) and _positive_number(point.get('loadW')) is not None
                        and _positive_number(point.get('loadW')) > curve_cap for point in points):
                    overrated_configs.add(config['id'])
                    _remember_conflicts(product, [{'field': 'runtime', 'configurationId': config['id']}],
                                        manufacturer)
            if overrated_configs:
                _invalidate_list(product, 'battery_configurations', lambda item:
                                 isinstance(item, dict) and item.get('id') in overrated_configs)
                _warning(product, 'Runtime exceeds a conflicting or verified capacity; configuration withheld')

        outlet_conflicts = [item for item, kind in parsed if kind == 'outlets']
        block_all_outlets = any(not isinstance(item, dict) or not _text(item.get('outletType'))
                                for item in outlet_conflicts)
        blocked_outlet_types = {item['outletType'] for item in outlet_conflicts
                                if isinstance(item, dict) and _text(item.get('outletType'))}
        if block_all_outlets:
            _invalidate_list(product, 'outlet_groups')
        elif blocked_outlet_types:
            _invalidate_list(product, 'outlet_groups', lambda item:
                             isinstance(item, dict) and item.get('type') in blocked_outlet_types)
        group_values = record.get('outletGroups')
        if isinstance(group_values, list) and not block_all_outlets:
            groups = [_group(value, manufacturer) for value in group_values]
            groups = [group for group in groups if group and group['type'] not in blocked_outlet_types]
            _merge(product, 'outlet_groups', groups, lambda value: (value.get('type'), value.get('power')),
                   'outlets')

        runtime_conflicts = [item for item, kind in parsed if kind == 'runtime']
        block_all_runtime = any(not isinstance(item, dict) or not _text(item.get('configurationId'))
                                for item in runtime_conflicts)
        blocked_configs = {item['configurationId'] for item in runtime_conflicts
                           if isinstance(item, dict) and _text(item.get('configurationId'))} | overrated_configs
        if block_all_runtime:
            _invalidate_list(product, 'battery_configurations')
        elif blocked_configs:
            _invalidate_list(product, 'battery_configurations', lambda item:
                             isinstance(item, dict) and item.get('id') in blocked_configs)
        values = record.get('batteryConfigurations')
        if isinstance(values, list) and not block_all_runtime:
            rated = _positive_number(product.get('rating_w')) if product.get('rating_w_verified') is True else None
            configurations = [_configuration(value, manufacturer, rated) for value in values]
            configurations = [value for value in configurations if value and value['id'] not in blocked_configs]
            _merge(product, 'battery_configurations', configurations, lambda value: value.get('id'), 'runtime')
    return result
