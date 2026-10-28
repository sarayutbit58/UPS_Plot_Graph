"""Shared, conservative THB ex-VAT price comparison policy."""

from datetime import date
from decimal import Decimal
import math
import sys

USER_CONFIRMED_PRICE_CURRENCY = 'THB'
VERIFIED_THAI_VAT_PERIODS = (
    (date(2025, 10, 1), date(2027, 9, 30), Decimal('7')),
)


def _representable_price(amount):
    try:
        number = float(amount)
    except (OverflowError, ValueError):
        return False
    return math.isfinite(number) and 0 < number <= sys.float_info.max / 10000


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
    if any(not isinstance(price, dict) or not isinstance(price.get('kind'), str)
           or price.get('kind') not in {'Dealer', 'NDP'}
           for price in prices):
        return unavailable('missing_price_kind', 'มีราคาที่ยังไม่ยืนยันว่าเป็น Dealer หรือ NDP')
    if any(not isinstance(price.get('source'), dict) or not any(
            isinstance(price['source'].get(key), str) and price['source'][key].strip()
            for key in ('sourceId', 'path', 'url')) for price in prices):
        return unavailable('missing_price_source', 'มีราคาที่ยังไม่มีแหล่งอ้างอิงให้ตรวจสอบ')
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
            if not amount.is_finite() or not _representable_price(amount):
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
            if not _representable_price(net_amount):
                return unavailable('missing_price', 'ราคา UPS บางรายการไม่ใช่จำนวนเงินที่ใช้เปรียบเทียบได้',
                                   currency, currency_source, missing_currency_count)
            normalized_offers.append((net_amount, offer))
        normalized_groups.append((candidate, min(
            normalized_offers, key=lambda item: (item[0], item[1].get('kind') or '')
        )))

    for candidate, (net_amount, offer) in normalized_groups:
        candidate['_cost'] = net_amount
        candidate['_comparison_offer'] = offer
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
