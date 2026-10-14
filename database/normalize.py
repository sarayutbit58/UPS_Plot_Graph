"""Conservative normalization; unresolved identities become explicit rejections."""

from datetime import date
from decimal import Decimal, InvalidOperation
import json
import math
from pathlib import PurePosixPath
import re
import unicodedata


TOPOLOGIES = {
    'offline': 'offline', 'standby': 'offline',
    'line-interactive': 'line-interactive', 'line interactive': 'line-interactive',
    'online-double-conversion': 'online-double-conversion',
    'online double conversion': 'online-double-conversion',
    'double conversion': 'online-double-conversion',
}
FORM_FACTORS = {
    'tower': 'tower', 'rackmount': 'rackmount', 'rack mount': 'rackmount',
    'rack-tower': 'rack-tower', 'rack/tower': 'rack-tower',
}
CURRENCIES = {'THB', 'USD', 'EUR'}
UNKNOWN = {'', 'unknown', 'n/a', 'na', 'none', 'null', '?', '-', 'ไม่ทราบ'}


def _text(value):
    if value is None:
        return ''
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFC', str(value))).strip()


def _positive(value, field):
    if value is None or value == '':
        return None
    if isinstance(value, bool):
        raise ValueError(f'invalid {field}')
    try:
        number = float(value)
    except (ValueError, TypeError):
        raise ValueError(f'invalid {field}') from None
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f'invalid {field}')
    return number


def normalize_record(raw):
    """Return accepted=True with normalized fields, or accepted=False with reason.

    Missing values remain null. Price strings must be plain decimal numbers;
    separators, currency symbols, floats and inferred currencies are rejected.
    All original assertions remain available in raw_json, including price tiers.
    """
    raw_json = json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str)
    source_path = unicodedata.normalize('NFC', str(raw.get('source_path') or '')).replace('\\', '/')
    source_locator = unicodedata.normalize('NFC', str(raw.get('source_locator') or ''))
    result = dict(accepted=False, source_path=source_path,
                  source_locator=source_locator, raw_json=raw_json)
    try:
        if not source_path.strip() or PurePosixPath(source_path).is_absolute() or '..' in PurePosixPath(source_path).parts or re.match(r'^[A-Za-z]:', source_path):
            raise ValueError('missing or non-relative source_path')
        if not source_locator.strip():
            raise ValueError('missing source_locator')
        manufacturer = _text(raw.get('manufacturer'))
        model = _text(raw.get('model'))
        sku = _text(raw.get('manufacturer_sku')) or None
        if not sku and _text(raw.get('sku_type')).casefold() == 'manufacturer':
            sku = _text(raw.get('sku')) or None
        notes = raw.get('extraction_notes') or []
        notes_text = str(notes).casefold()
        if raw.get('identity_ambiguous') or raw.get('model_conflict') or any(token in notes_text for token in ('identity_conflict', 'model_conflict', 'brand_conflict')):
            raise ValueError('ambiguous product identity')
        if raw.get('non_ups_category') or 'non_ups_category' in notes_text:
            raise ValueError('explicit non-UPS category')
        if manufacturer.casefold() in UNKNOWN or any(separator in manufacturer for separator in ('/', '|', ';')):
            raise ValueError('unknown or ambiguous manufacturer')
        if model.casefold() in UNKNOWN or any(separator in model for separator in ('|', ';')):
            raise ValueError('unknown or ambiguous model')
        if sku and sku.casefold() in UNKNOWN:
            sku = None
        product_type = _text(raw.get('product_type'))
        product_type = {'ups': 'UPS', 'battery': 'battery', 'accessory': 'accessory'}.get(product_type.casefold())
        if not product_type:
            raise ValueError('missing or unsupported product_type')
        manufacturer_key = manufacturer.casefold()
        identity_key = json.dumps([manufacturer_key, 'sku' if sku else 'model', sku or model], ensure_ascii=False, separators=(',', ':'))
        price = raw.get('price_amount')
        amount = None
        if price is not None and price != '':
            if not isinstance(price, (str, int)) or isinstance(price, bool) or not re.fullmatch(r'\d+(?:\.\d+)?', str(price)):
                raise ValueError('price_amount must be an exact nonnegative decimal string')
            try:
                decimal = Decimal(str(price))
            except InvalidOperation:
                raise ValueError('invalid price_amount') from None
            if not decimal.is_finite() or decimal < 0:
                raise ValueError('invalid price_amount')
            amount = format(decimal, 'f')
        currency = _text(raw.get('currency')).upper() or None
        if currency and currency not in CURRENCIES:
            raise ValueError('unsupported explicit currency')
        vat = _text(raw.get('vat_treatment')) or 'unknown'
        if vat not in {'unknown', 'exclusive', 'inclusive'}:
            raise ValueError('unsupported vat_treatment')
        price_type = _text(raw.get('price_type'))
        price_type = 'unknown' if price_type.casefold() in UNKNOWN else 'source-marked'
        source_date = _text(raw.get('source_date')) or None
        if source_date:
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', source_date):
                raise ValueError('source_date must be an ISO calendar date')
            try:
                date.fromisoformat(source_date)
            except ValueError:
                raise ValueError('source_date must be an ISO calendar date') from None
        result.update(
            accepted=True, manufacturer=manufacturer, manufacturer_key=manufacturer_key,
            identity_key=identity_key, model=model, sku=sku,
            description=_text(raw.get('description')) or None, product_type=product_type,
            raw_text=str(raw.get('raw_text') or ''),
            rating_va=_positive(raw.get('rating_va'), 'rating_va'),
            rating_w=_positive(raw.get('rating_w'), 'rating_w'),
            topology=TOPOLOGIES.get(_text(raw.get('topology')).casefold()),
            form_factor=FORM_FACTORS.get(_text(raw.get('form_factor')).casefold()),
            price_amount=amount, currency=currency, vat_treatment=vat,
            price_type=price_type, source_date=source_date,
            confidence=raw.get('confidence'), extraction_notes=notes,
        )
    except ValueError as error:
        result = dict(accepted=False, reason=str(error), source_path=source_path,
                      source_locator=source_locator, raw_json=raw_json)
    return result
