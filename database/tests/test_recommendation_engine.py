"""Acceptance cases for database-backed UPS recommendations."""

import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from recommendation_engine import RecommendationInputError, recommend_from_connection
from verified_evidence import MANUFACTURER_DOMAINS
from verified_fixture import verified_curve


def add_model_only_alias(connection, manufacturer_id, source_file_id):
    """Add an intentionally unresolved bundle/alias only in the targeted tests."""
    alias_id = connection.execute('''INSERT INTO products
        (manufacturer_id,identity_key,model,sku,product_type) VALUES (?,?,?,?,?)''',
        (manufacturer_id, 'model-only-alias:UPS-1', 'UPS-1', None, 'UPS')).lastrowid
    alias_raw = {'manufacturer': 'Test Vendor', 'model': 'UPS-1', 'product_type': 'UPS',
                 'source_path': 'SYNTHETIC/catalog.xlsx', 'source_locator': 'sheet:Alias;row:1'}
    alias_source = connection.execute('''INSERT INTO product_sources
        (product_id,source_file_id,source_locator,raw_text,raw_json) VALUES (?,?,?,?,?)''',
        (alias_id, source_file_id, alias_raw['source_locator'], 'model-only alias',
         json.dumps(alias_raw, ensure_ascii=False))).lastrowid
    connection.execute('''INSERT INTO product_specifications
        (product_source_id,rating_va,rating_w,topology,form_factor) VALUES (?,?,?,?,?)''',
        (alias_source, 900, 700, 'line-interactive', 'tower'))
    return alias_id


def seed_catalog(connection):
    database = Path(__file__).resolve().parents[1]
    connection.row_factory = sqlite3.Row
    connection.executescript((database / 'schema.sql').read_text(encoding='utf-8'))
    connection.executescript((database / 'seeds/001_reference.sql').read_text(encoding='utf-8'))
    connection.execute("INSERT INTO manufacturers(name,canonical_key) VALUES ('Test Vendor','test-vendor')")
    manufacturer_id = connection.execute('SELECT id FROM manufacturers').fetchone()[0]
    connection.execute("INSERT INTO source_files(relative_path,extension,size_bytes,status,scanned_at) VALUES ('SYNTHETIC/catalog.xlsx','.xlsx',10,'imported','2026-01-01')")
    source_file_id = connection.execute('SELECT id FROM source_files').fetchone()[0]
    products = []

    def add_product(model, rating_va, rating_w, outlets, full_runtime, half_runtime,
                    expansion, price, price_type='Dealer Price', max_expansion=None):
        product_id = connection.execute('''INSERT INTO products
            (manufacturer_id,identity_key,model,sku,description,product_type)
            VALUES (?,?,?,?,?,'UPS')''', (manufacturer_id, model, model, model, model)).lastrowid
        values = [model, str(rating_va), str(rating_w), str(outlets),
                  f'{full_runtime} min', f'{half_runtime} min', expansion,
                  '' if max_expansion is None else str(max_expansion)]
        headers = ['Part Number', 'VA', 'Watts', 'Outlets', 'Full load', 'Half load',
                   'External Battery', 'Maximum Pack of Battery']
        raw = {'manufacturer': 'Test Vendor', 'model': model, 'sku': model,
               'product_type': 'UPS', 'rating_va': rating_va, 'rating_w': rating_w,
               'source_path': 'SYNTHETIC/catalog.xlsx', 'source_locator': f'sheet:UPS;row:{product_id}',
               'source_headers': headers, 'price_amount': price, 'currency': None,
               'price_type': price_type, 'vat_treatment': 'exclusive'}
        source_id = connection.execute('''INSERT INTO product_sources
            (product_id,source_file_id,source_locator,raw_text,raw_json) VALUES (?,?,?,?,?)''',
            (product_id, source_file_id, raw['source_locator'], ' | '.join(values),
             json.dumps(raw, ensure_ascii=False))).lastrowid
        connection.execute('''INSERT INTO product_specifications
            (product_source_id,rating_va,rating_w,topology,form_factor) VALUES (?,?,?,?,?)''',
            (source_id, rating_va, rating_w, 'line-interactive', 'tower'))
        connection.execute('''INSERT INTO price_offers
            (product_source_id,amount_decimal,currency,vat_treatment,price_type)
            VALUES (?,?,?,?,?)''', (source_id, price, None, 'exclusive', 'source-marked'))
        products.append(product_id)
        return product_id

    for index in range(6):
        add_product(
            model=f'UPS-{index + 1}', rating_va=900 + 100 * index,
            rating_w=700 + 100 * index, outlets=4 + index,
            full_runtime=6 + index, half_runtime=14 + index,
            expansion='EBM-1' if index % 2 == 0 else 'N/A',
            max_expansion=2 if index % 2 == 0 else None,
            price=str(9000 + 1000 * index), price_type='NDP (Exc.VAT)',
        )
    add_product('UPS-TOO-SMALL', rating_va=550, rating_w=440, outlets=4,
                full_runtime=3, half_runtime=8, expansion='N/A', price='1000')
    product_id = connection.execute("SELECT id FROM products WHERE model='UPS-1'").fetchone()[0]
    warranty_raw = {'manufacturer': 'Test Vendor', 'model': 'UPS-1', 'product_type': 'UPS',
                    'price_type': 'Dealer Price', 'currency': None,
                    'source_path': 'SYNTHETIC/catalog.xlsx',
                    'source_locator': 'sheet:Warranty upgrade;row:1'}
    warranty_source = connection.execute('''INSERT INTO product_sources
        (product_id,source_file_id,source_locator,raw_text,raw_json) VALUES (?,?,?,?,?)''',
        (product_id, source_file_id, warranty_raw['source_locator'],
         'UPS-1 | WEXT1YR-SU-02 | Warranty upgrade | add one year warranty | 1',
         json.dumps(warranty_raw, ensure_ascii=False))).lastrowid
    connection.execute('''INSERT INTO product_specifications
        (product_source_id,rating_va,rating_w,topology,form_factor) VALUES (?,?,?,?,?)''',
        (warranty_source, 1000, 700, 'line-interactive', 'tower'))
    connection.execute('''INSERT INTO price_offers
        (product_source_id,amount_decimal,currency,vat_treatment,price_type)
        VALUES (?,?,?,?,?)''', (warranty_source, '1', None, 'unknown', 'source-marked'))
    total_raw = {'manufacturer': 'Test Vendor', 'model': 'UPS-1', 'product_type': 'UPS',
                 'price_type': 'Total NDP', 'currency': None,
                 'source_path': 'SYNTHETIC/catalog.xlsx',
                 'source_locator': 'sheet:Total NDP;row:1'}
    total_source = connection.execute('''INSERT INTO product_sources
        (product_id,source_file_id,source_locator,raw_text,raw_json) VALUES (?,?,?,?,?)''',
        (product_id, source_file_id, total_raw['source_locator'],
         'UPS-1 | Total NDP for multiple units | 2',
         json.dumps(total_raw, ensure_ascii=False))).lastrowid
    connection.execute('''INSERT INTO product_specifications
        (product_source_id,rating_va,rating_w,topology,form_factor) VALUES (?,?,?,?,?)''',
        (total_source, 900, 700, 'line-interactive', 'tower'))
    connection.execute('''INSERT INTO price_offers
        (product_source_id,amount_decimal,currency,vat_treatment,price_type)
        VALUES (?,?,?,?,?)''', (total_source, '2', None, 'unknown', 'source-marked'))
    return {'products': products, 'manufacturer_id': manufacturer_id, 'source_file_id': source_file_id}


class RecommendationEngineTests(unittest.TestCase):
    def setUp(self):
        domains = patch.dict(MANUFACTURER_DOMAINS, {'test vendor': ('vendor.example',)})
        domains.start()
        self.addCleanup(domains.stop)
        self.connection = sqlite3.connect(':memory:', isolation_level=None)
        self.catalog = seed_catalog(self.connection)
        self.curves = [verified_curve(
            f'UPS-{index + 1}', 700 + 100 * index, 900 + 100 * index, 4 + index,
            [{'loadW': (700 + 100 * index) / 2, 'runtimeMinutes': 14 + index},
             {'loadW': 700 + 100 * index, 'runtimeMinutes': 6 + index}],
        ) for index in range(6)]

    def tearDown(self):
        self.connection.close()

    def request(self, **updates):
        value = {
            'devices': [{'name': 'PoE Switch', 'quantity': 2, 'watt': 200, 'va': 250}],
            'powerFactor': 0.8,
            'runtimeRequirementMinutes': 10,
        }
        value.update(updates)
        return value

    def recommend(self, request, curves=None):
        return recommend_from_connection(self.connection, request,
            curve_catalog=self.curves if curves is None else curves)

    def test_recommendation_does_not_certify_legacy_runtime_without_battery_setup(self):
        curve = {'manufacturer': 'Test Vendor', 'model': 'UPS-1', 'sku': 'UPS-1',
                 'identityVerified': True,
                 'ratingWattsVerified': True, 'ratedWatts': 700,
                 'ratingVaVerified': True, 'ratedVa': 900,
                 'points': [{'loadW': 100, 'runtimeMinutes': 30},
                            {'loadW': 700, 'runtimeMinutes': 10}],
                 'outletGroups': [{'type': 'IEC_C13', 'power': 'battery', 'count': 4}],
                 'source': {'url': 'https://vendor.example/ups-1.pdf'}}
        result = recommend_from_connection(self.connection, self.request(), curve_catalog=[curve])
        candidate = next(row for row in result['recommendations'] if row['model'] == 'UPS-1')
        self.assertEqual('unknown', candidate['runtimeStatus'])
        self.assertIsNone(candidate['estimatedRuntimeMinutes'])

    def test_unverified_models_do_not_appear_as_recommendations(self):
        result = recommend_from_connection(self.connection, self.request(), curve_catalog=[])
        self.assertEqual([], result['recommendations'])

    def test_recommendation_does_not_use_unverified_legacy_capacity(self):
        curve = {'manufacturer': 'Test Vendor', 'model': 'UPS-1', 'sku': 'UPS-1',
                 'identityVerified': True,
                 'points': [{'loadW': 100, 'runtimeMinutes': 30}, {'loadW': 700, 'runtimeMinutes': 10}],
                 'source': {'url': 'https://vendor.example/ups-1.pdf'}}
        result = recommend_from_connection(self.connection, self.request(), curve_catalog=[curve])
        self.assertEqual([], result['recommendations'])

    def test_recommendation_does_not_rank_base_price_as_total_with_extra_battery(self):
        self.connection.execute("UPDATE price_offers SET currency='THB'")
        curve = {'manufacturer': 'Test Vendor', 'model': 'UPS-1', 'sku': 'UPS-1',
                 'identityVerified': True, 'ratingWattsVerified': True, 'ratedWatts': 700,
                 'ratingVaVerified': True, 'ratedVa': 900,
                 'points': [{'loadW': 1, 'runtimeMinutes': 60}, {'loadW': 700, 'runtimeMinutes': 20}],
                 'outletGroups': [{'type': 'IEC_C13', 'power': 'battery',
                                   'manufacturer_verified': True, 'count': 4}],
                 'batteryConfigurations': [{
                     'id': 'plus-one', 'label': 'One extra battery', 'manufacturer_verified': True,
                     'points': [{'loadW': 1, 'runtimeMinutes': 60},
                                {'loadW': 700, 'runtimeMinutes': 20}],
                     'includedPacks': [], 'extraPacks': [{'sku': 'EBM-1', 'quantity': 1}],
                 }], 'source': {'url': 'https://vendor.example/ups-1.pdf'}}
        result = recommend_from_connection(self.connection, self.request(), curve_catalog=[curve])
        candidate = next(row for row in result['recommendations'] if row['model'] == 'UPS-1')
        self.assertIsNone(candidate['scores']['price'])
        self.assertTrue(any('battery' in warning.casefold() for warning in candidate['warnings']))

    def test_returns_top_five_explainable_recommendations_and_preserves_provenance(self):
        result = self.recommend(self.request())
        self.assertEqual(5, len(result['recommendations']))
        self.assertEqual(6, result['eligibleProductCount'])
        first = result['recommendations'][0]
        self.assertEqual({'capacity', 'runtime', 'outlet', 'price', 'expansion'}, set(first['scores']))
        self.assertEqual('meets_runtime', first['runtimeStatus'])
        self.assertTrue(first['reasons'])
        self.assertIn('source', first['estimatedResellerCost'])
        self.assertIsNone(first['estimatedResellerCost']['currency'])
        self.assertIsNone(first['scores']['price'])
        self.assertEqual(2, result['load']['requiredOutlets'])
        self.assertTrue(first['technicalEvidence']['ratingVa'])
        self.assertTrue(first['technicalEvidence']['runtimeCurve'])
        for field in ('ratingVa', 'ratingWatts', 'outlets', 'runtimeCurve'):
            with self.subTest(field=field):
                self.assertTrue(first['technicalEvidence'][field])
                self.assertTrue(all(source['url'].startswith('https://vendor.example/')
                                    for source in first['technicalEvidence'][field]))
                self.assertNotIn('synthetic/', json.dumps(first['technicalEvidence'][field]).casefold())

    def test_does_not_treat_warranty_upgrade_price_as_ups_reseller_cost(self):
        result = self.recommend(self.request())
        candidate = next(row for row in result['recommendations'] if row['model'] == 'UPS-1')
        self.assertEqual('9000', candidate['estimatedResellerCost']['amount'])
        self.assertNotIn('Warranty upgrade', candidate['estimatedResellerCost']['source']['locator'])
        self.assertNotIn('Total NDP', candidate['estimatedResellerCost']['source']['locator'])

    def test_price_efficiency_is_scored_only_when_currency_is_comparable(self):
        self.connection.execute("UPDATE price_offers SET currency='THB'")
        result = self.recommend(self.request())
        self.assertTrue(all(row['scores']['price'] is not None for row in result['recommendations']))
        self.assertEqual('THB', result['recommendations'][0]['estimatedResellerCost']['currency'])

    def test_model_only_alias_is_not_recommended_until_bundle_identity_is_confirmed(self):
        add_model_only_alias(self.connection, self.catalog['manufacturer_id'],
                             self.catalog['source_file_id'])
        result = self.recommend(self.request())
        self.assertNotIn('UPS-1', [row['model'] for row in result['recommendations']])
        self.assertEqual(5, result['eligibleProductCount'])
        self.assertGreaterEqual(result['excludedProductCount']['unverifiedIdentity'], 1)

    def test_aggregates_quantity_and_converts_missing_units_using_power_factor(self):
        result = self.recommend(self.request(devices=[
            {'name': 'Server', 'quantity': 2, 'watt': 100},
            {'name': 'Router', 'quantity': 1, 'va': 200},
        ]))
        self.assertEqual(360, result['load']['watts'])
        self.assertEqual(450, result['load']['va'])
        self.assertEqual(3, result['load']['requiredOutlets'])

    def test_uses_both_va_and_watt_margin_and_excludes_under_capacity_products(self):
        result = self.recommend(self.request())
        self.assertNotIn('UPS-TOO-SMALL', [row['model'] for row in result['recommendations']])
        for row in result['recommendations']:
            self.assertGreaterEqual(row['engineeringMargin']['wattsPercent'], 20)
            self.assertGreaterEqual(row['engineeringMargin']['vaPercent'], 20)

    def test_excludes_battery_pack_misclassified_as_ups(self):
        product_id = self.connection.execute('''INSERT INTO products
            (manufacturer_id,identity_key,model,sku,description,product_type) VALUES (?,?,?,?,?,'UPS')''',
            (self.catalog['manufacturer_id'], 'MISCLASSIFIED-EBM', 'SRV6KIL', 'APC-SRV240BP-9A',
             'APC Easy UPS SRV 240V Battery Pack for 6&10kVA Tower')).lastrowid
        tech = {'manufacturer': 'APC', 'model': 'SRV6KIL', 'product_type': 'ups',
                'source_path': 'SYNTHETIC/catalog.xlsx',
                'source_locator': 'sheet:How to Select UPS;row:70'}
        tech_source = self.connection.execute('''INSERT INTO product_sources
            (product_id,source_file_id,source_locator,raw_text,raw_json) VALUES (?,?,?,?,?)''',
            (product_id, self.catalog['source_file_id'], tech['source_locator'],
             'SRV6KIL | Double-conversion online | 6000 | 6000 | 6 min | 17 min',
             json.dumps(tech))).lastrowid
        self.connection.execute('''INSERT INTO product_specifications
            (product_source_id,rating_va,rating_w) VALUES (?,?,?)''', (tech_source, 6000, 6000))
        accessory = {'manufacturer': 'APC', 'model': 'SRV6KIL', 'sku': 'APC-SRV240BP-9A',
                     'description': 'APC Easy UPS SRV 240V Battery Pack for 6&10kVA Tower',
                     'price_type': 'NDP (Exc VAT)', 'source_path': 'SYNTHETIC/catalog.xlsx',
                     'source_locator': 'sheet:Dealer;row:80'}
        price_source = self.connection.execute('''INSERT INTO product_sources
            (product_id,source_file_id,source_locator,raw_text,raw_json) VALUES (?,?,?,?,?)''',
            (product_id, self.catalog['source_file_id'], accessory['source_locator'],
             accessory['description'], json.dumps(accessory))).lastrowid
        self.connection.execute('''INSERT INTO product_specifications
            (product_source_id,rating_va,rating_w) VALUES (?,?,?)''', (price_source, 10000, None))
        self.connection.execute('''INSERT INTO price_offers
            (product_source_id,amount_decimal,currency,vat_treatment,price_type)
            VALUES (?,?,?,?,?)''', (price_source, '32530', None, 'exclusive', 'source-marked'))
        result = self.recommend(self.request(devices=[
            {'name': 'Large load', 'quantity': 1, 'watt': 4000, 'va': 5000},
        ]))
        self.assertNotIn('SRV6KIL', [row['model'] for row in result['recommendations']])
        self.assertGreater(result['excludedProductCount']['nonUpsAccessory'], 0)

    def test_keeps_ups_system_sold_with_external_battery_pack(self):
        product_id = self.connection.execute('''INSERT INTO products
            (manufacturer_id,identity_key,model,sku,description,product_type) VALUES (?,?,?,?,?,'UPS')''',
            (self.catalog['manufacturer_id'], 'BUNDLED-UPS', 'SRV6KIL-BUNDLE', 'SRV6KIL-BUNDLE',
             'APC Easy UPS SRV 6000VA 230V, with RailKit, External Battery Pack')).lastrowid
        source_id = self.connection.execute('''INSERT INTO product_sources
            (product_id,source_file_id,source_locator,raw_text,raw_json) VALUES (?,?,?,?,?)''',
            (product_id, self.catalog['source_file_id'], 'sheet:UPS;row:bundle',
             'APC Easy UPS SRV 6000VA 230V, with RailKit, External Battery Pack',
             json.dumps({'manufacturer': 'APC', 'model': 'SRV6KIL-BUNDLE'}))).lastrowid
        self.connection.execute('''INSERT INTO product_specifications
            (product_source_id,rating_va,rating_w) VALUES (?,?,?)''', (source_id, 6000, 6000))
        bundle = verified_curve('SRV6KIL-BUNDLE', 6000, 6000, 4,
                                [{'loadW': 1, 'runtimeMinutes': 60},
                                 {'loadW': 6000, 'runtimeMinutes': 20}])
        bundle['batteryConfigurations'][0]['label'] = 'Synthetic bundle with included battery pack'
        bundle['batteryConfigurations'][0]['includedPacks'] = [{'sku': 'EBM-1', 'quantity': 1}]
        result = self.recommend(self.request(), curves=self.curves + [bundle])
        self.assertEqual(7, result['eligibleProductCount'])
        self.assertEqual(0, result['excludedProductCount']['nonUpsAccessory'])

    def test_rejects_loads_that_overflow_json_number_range(self):
        with self.assertRaisesRegex(RecommendationInputError, 'supported numeric range'):
            recommend_from_connection(self.connection, self.request(devices=[
                {'name': 'Large load A', 'quantity': 1, 'watt': '1e308', 'va': '1e308'},
                {'name': 'Large load B', 'quantity': 1, 'watt': '1e308', 'va': '1e308'},
            ]))

    def test_runtime_uses_curve_interpolation_and_never_extrapolates(self):
        result = self.recommend(self.request(
            devices=[{'name': 'Load', 'quantity': 1, 'watt': 600, 'va': 750}],
            runtimeRequirementMinutes=30,
        ))
        row = next(item for item in result['recommendations'] if item['model'] == 'UPS-2')
        self.assertEqual(11, row['estimatedRuntimeMinutes'])
        self.assertEqual('below_runtime_requirement', row['runtimeStatus'])
        self.assertEqual('https://vendor.example/ups-2.pdf', row['runtimeCurve']['source']['url'])

    def test_runtime_is_unknown_below_the_lowest_published_curve_load(self):
        result = self.recommend(self.request(
            devices=[{'name': 'Small load', 'quantity': 1, 'watt': 100, 'va': 125}],
        ))
        self.assertEqual(5, len(result['recommendations']))
        self.assertTrue(all(row['estimatedRuntimeMinutes'] is None for row in result['recommendations']))
        self.assertTrue(all(row['runtimeStatus'] == 'unknown' for row in result['recommendations']))

    def test_exact_manufacturer_curve_overrides_local_two_point_curve(self):
        official = verified_curve('UPS-1', 700, 900, 8, [
            {'loadW': 200, 'runtimeMinutes': 40},
            {'loadW': 500, 'runtimeMinutes': 10},
            {'loadW': 700, 'runtimeMinutes': 6},
        ])
        official['source'] = {'url': 'https://vendor.example/runtime.pdf', 'publicationYear': 2026}
        official['outletGroups'][0]['source'] = official['source']
        official['batteryConfigurations'][0]['source'] = official['source']
        official_curve = [official]
        result = recommend_from_connection(self.connection, self.request(), curve_catalog=official_curve)
        row = next(item for item in result['recommendations'] if item['model'] == 'UPS-1')
        self.assertEqual(20, row['estimatedRuntimeMinutes'])
        self.assertEqual(8, row['outletFitness']['available'])
        self.assertEqual('https://vendor.example/runtime.pdf', row['runtimeCurve']['source']['url'])

    def test_manufacturer_curve_is_not_matched_by_model_when_sku_conflicts(self):
        wrong = verified_curve('UPS-1', 700, 900, 4,
                               [{'loadW': 200, 'runtimeMinutes': 40},
                                {'loadW': 700, 'runtimeMinutes': 6}])
        wrong['sku'] = 'OTHER-SKU'
        valid_identity_without_runtime = {**self.curves[0], 'batteryConfigurations': []}
        result = recommend_from_connection(self.connection, self.request(),
                                           curve_catalog=[wrong, valid_identity_without_runtime])
        row = next(item for item in result['recommendations'] if item['model'] == 'UPS-1')
        self.assertIsNone(row['estimatedRuntimeMinutes'])
        self.assertEqual('unknown', row['runtimeStatus'])
        self.assertIsNone(row['runtimeCurve'])

    def test_marks_runtime_and_outlet_unknown_when_source_evidence_is_missing(self):
        # A candidate with no curve or outlet evidence remains explicit about the gap.
        product_id = self.connection.execute('''INSERT INTO products
            (manufacturer_id,identity_key,model,sku,product_type) VALUES (?,?,?,?,?)''',
            (self.catalog['manufacturer_id'], 'UPS-NO-EVIDENCE', 'UPS-NO-EVIDENCE', 'UPS-NO-EVIDENCE', 'UPS')).lastrowid
        raw = {'manufacturer': 'Test Vendor', 'model': 'UPS-NO-EVIDENCE', 'price_type': 'NDP', 'currency': None}
        source_id = self.connection.execute('''INSERT INTO product_sources
            (product_id,source_file_id,source_locator,raw_text,raw_json) VALUES (?,?,?,?,?)''',
            (product_id, self.catalog['source_file_id'], 'sheet:UPS;row:no-evidence', '',
             json.dumps(raw))).lastrowid
        self.connection.execute('''INSERT INTO product_specifications
            (product_source_id,rating_va,rating_w,topology,form_factor) VALUES (?,?,?,?,?)''',
            (source_id, 5000, 4000, None, None))
        self.connection.execute('''INSERT INTO price_offers
            (product_source_id,amount_decimal,currency,vat_treatment,price_type)
            VALUES (?,?,?,?,?)''', (source_id, '50000', None, 'exclusive', 'source-marked'))
        no_runtime_or_outlets = verified_curve('UPS-NO-EVIDENCE', 4000, 5000, 4,
                                              [{'loadW': 1, 'runtimeMinutes': 60},
                                               {'loadW': 4000, 'runtimeMinutes': 20}])
        no_runtime_or_outlets.pop('batteryConfigurations')
        no_runtime_or_outlets.pop('outletGroups')
        no_runtime_or_outlets.pop('outletCount')
        result = self.recommend(self.request(devices=[
            {'name': 'Large load', 'quantity': 1, 'watt': 3200, 'va': 4000},
        ]), curves=[no_runtime_or_outlets])
        row = next(item for item in result['recommendations'] if item['model'] == 'UPS-NO-EVIDENCE')
        self.assertIsNone(row['estimatedRuntimeMinutes'])
        self.assertEqual('unknown', row['runtimeStatus'])
        self.assertIsNone(row['scores']['outlet'])

    def test_rejects_invalid_loads_and_power_factors(self):
        for request in (
            self.request(devices=[]),
            self.request(devices=[{'name': '', 'quantity': 1, 'watt': 50}]),
            self.request(devices=[{'name': 'Bad quantity', 'quantity': 0, 'watt': 50}]),
            self.request(powerFactor=0),
            self.request(runtimeRequirementMinutes=-1),
        ):
            with self.subTest(request=request), self.assertRaises(RecommendationInputError):
                recommend_from_connection(self.connection, request)


if __name__ == '__main__':
    unittest.main()
