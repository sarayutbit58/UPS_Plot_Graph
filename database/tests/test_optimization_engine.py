"""Acceptance tests for the bounded UPS design optimizer."""

from pathlib import Path
import json
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_recommendation_engine import seed_catalog
from verified_fixture import verified_curve
from verified_evidence import MANUFACTURER_DOMAINS

try:
    from optimization_engine import OptimizationInputError, optimize_from_connection
except ImportError:
    OptimizationInputError = ValueError
    optimize_from_connection = None


class OptimizationEngineTests(unittest.TestCase):
    def test_optimizer_excludes_outlets_without_battery_backup_evidence(self):
        curve = {**self.curves[0]}
        curve.pop('outletGroups')
        result = optimize_from_connection(self.connection, {
            'selectedProductIds': [self.catalog['products'][0]],
            'runtimeRequirementMinutes': 10,
            'devices': [{'name': 'Server', 'quantity': 1, 'watt': 400, 'va': 500, 'priority': 50}],
        }, curve_catalog=[curve])
        self.assertIsNone(result['bestSolution'])
        self.assertEqual(0, result['search']['candidateModelCount'])
        self.assertIn('outlet_unknown', result['selectionDiagnostics']['excludedModels'][0]['reasons'])

    def test_optimizer_excludes_unverified_identity(self):
        curve = {**self.curves[0], 'identityVerified': False, 'outletGroups': [
            {'type': 'IEC_C13', 'power': 'battery', 'manufacturer_verified': True, 'count': 8},
        ]}
        result = optimize_from_connection(self.connection, {
            'selectedProductIds': [self.catalog['products'][0]],
            'runtimeRequirementMinutes': 10,
            'devices': [{'name': 'Server', 'quantity': 1, 'watt': 400, 'va': 500, 'priority': 50}],
        }, curve_catalog=[curve])
        self.assertIsNone(result['bestSolution'])
        self.assertEqual(0, result['search']['candidateModelCount'])
        self.assertIn('identity_unverified', result['selectionDiagnostics']['excludedModels'][0]['reasons'])

    def test_optimizer_excludes_runtime_without_verified_battery_setup(self):
        curve = {**self.curves[0], 'identityVerified': True, 'outletGroups': [
            {'type': 'IEC_C13', 'power': 'battery', 'manufacturer_verified': True, 'count': 8},
        ]}
        curve.pop('batteryConfigurations')
        result = optimize_from_connection(self.connection, {
            'selectedProductIds': [self.catalog['products'][0]],
            'runtimeRequirementMinutes': 10,
            'devices': [{'name': 'Server', 'quantity': 1, 'watt': 400, 'va': 500, 'priority': 50}],
        }, curve_catalog=[curve])
        self.assertIsNone(result['bestSolution'])
        self.assertEqual(0, result['search']['candidateModelCount'])
        self.assertIn('runtime_unknown', result['selectionDiagnostics']['excludedModels'][0]['reasons'])

    def test_optimizer_counts_all_plugs_without_splitting_each_device(self):
        curve = {**self.curves[0], 'identityVerified': True,
                 'ratingWattsVerified': True, 'ratedWatts': 700,
                 'ratingVaVerified': True, 'ratedVa': 900, 'outletGroups': [
            {'type': 'IEC_C13', 'power': 'battery', 'manufacturer_verified': True, 'count': 3},
        ], 'batteryConfigurations': [{
            'id': 'internal', 'manufacturer_verified': True, 'label': 'Internal', 'points': [
                {'loadW': 1, 'runtimeMinutes': 60}, {'loadW': 700, 'runtimeMinutes': 20}],
            'includedPacks': [], 'extraPacks': [],
        }]}
        result = optimize_from_connection(self.connection, {
            'selectedProductIds': [self.catalog['products'][0]],
            'runtimeRequirementMinutes': 10,
            'devices': [{'name': 'Dual PSU server', 'quantity': 2, 'watt': 100, 'va': 125,
                         'priority': 50, 'plugs': [{'connector': 'IEC_C14', 'count': 2}]}],
        }, curve_catalog=[curve])
        self.assertIsNotNone(result['bestSolution'])
        self.assertEqual(2, result['bestSolution']['unitCount'])
        self.assertEqual([2, 2], sorted(unit['outlets']['used'] for unit in result['bestSolution']['units']))

    def test_optimizer_excludes_legacy_capacity_without_manufacturer_verification(self):
        curve = {**self.curves[0], 'identityVerified': True, 'outletGroups': [
            {'type': 'IEC_C13', 'power': 'battery', 'manufacturer_verified': True, 'count': 3},
        ], 'batteryConfigurations': [{
            'id': 'internal', 'manufacturer_verified': True, 'label': 'Internal', 'points': [
                {'loadW': 1, 'runtimeMinutes': 60}, {'loadW': 700, 'runtimeMinutes': 20}],
            'includedPacks': [], 'extraPacks': [],
        }]}
        curve.pop('ratingWattsVerified')
        curve.pop('ratingVaVerified')
        result = optimize_from_connection(self.connection, {
            'selectedProductIds': [self.catalog['products'][0]],
            'runtimeRequirementMinutes': 10,
            'devices': [{'name': 'Server', 'quantity': 1, 'watt': 400, 'va': 500, 'priority': 50}],
        }, curve_catalog=[curve])
        self.assertEqual(0, result['search']['candidateModelCount'])
        reasons = result['selectionDiagnostics']['excludedModels'][0]['reasons']
        self.assertIn('watt_capacity_unknown', reasons)
        self.assertIn('va_capacity_unknown', reasons)

    def test_optimizer_can_select_battery_configuration_and_never_assume_pack_cost_is_zero(self):
        curve = {**self.curves[0], 'identityVerified': True,
                 'ratingWattsVerified': True, 'ratedWatts': 700,
                 'ratingVaVerified': True, 'ratedVa': 900, 'outletGroups': [
            {'type': 'IEC_C13', 'power': 'battery', 'manufacturer_verified': True, 'count': 4},
        ], 'batteryConfigurations': [
            {'id': 'internal', 'manufacturer_verified': True, 'label': 'Internal', 'points': [
                {'loadW': 1, 'runtimeMinutes': 8}, {'loadW': 700, 'runtimeMinutes': 2}],
             'includedPacks': [], 'extraPacks': []},
            {'id': 'plus-one', 'manufacturer_verified': True, 'label': 'Internal plus one pack', 'points': [
                {'loadW': 1, 'runtimeMinutes': 120}, {'loadW': 700, 'runtimeMinutes': 40}],
             'includedPacks': [], 'extraPacks': [{'sku': 'EBM-1', 'quantity': 1}]},
        ]}
        result = optimize_from_connection(self.connection, {
            'selectedConfigurations': [{'productId': self.catalog['products'][0],
                                        'batteryConfigId': 'plus-one'}],
            'runtimeRequirementMinutes': 10,
            'devices': [{'name': 'Server', 'quantity': 1, 'watt': 400, 'va': 500, 'priority': 50}],
        }, curve_catalog=[curve])
        self.assertIsNotNone(result['bestSolution'])
        self.assertEqual('plus-one', result['bestSolution']['models'][0]['batteryConfigId'])
        self.assertIsNone(result['bestSolution']['totalCost'])
        self.assertFalse(result['priceComparison']['available'])
        self.assertEqual('battery_pack_price_unknown', result['priceComparison']['reasonCode'])

    def test_selected_product_ids_restrict_solver_and_report_unavailable_models(self):
        selected = self.catalog['products'][0]
        result = optimize_from_connection(self.connection, {
            'selectedProductIds': [selected, 2147483647],
            'runtimeRequirementMinutes': 10,
            'devices': [{'name': 'Server', 'quantity': 1, 'watt': 400, 'va': 500, 'priority': 50}],
        }, curve_catalog=self.curves)
        self.assertEqual(1, result['search']['candidateModelCount'])
        self.assertEqual([{'productId': 2147483647, 'reasons': ['not_an_available_ups']}],
                         result['selectionDiagnostics']['excludedModels'])
        self.assertTrue(all(m['productId'] == selected for m in result['bestSolution']['models']))

    def test_rejects_invalid_selected_product_ids(self):
        for selected in ([], [True], [0], [1, 1], ['1'], list(range(1, 102))):
            with self.subTest(selected=selected), self.assertRaises(OptimizationInputError):
                optimize_from_connection(self.connection, {'selectedProductIds': selected},
                                         curve_catalog=self.curves)

    def test_selected_model_without_runtime_is_diagnosed_without_guessing(self):
        selected = self.catalog['products'][0]
        self.connection.execute('UPDATE product_sources SET raw_text=?', ('no runtime facts',))
        curve = {**self.curves[0]}
        curve.pop('batteryConfigurations')
        result = optimize_from_connection(self.connection, {
            'selectedProductIds': [selected], 'runtimeRequirementMinutes': 10,
            'devices': [{'name': 'Server', 'quantity': 1, 'watt': 400, 'va': 500, 'priority': 50}],
        }, curve_catalog=[curve])
        self.assertIsNone(result['bestSolution'])
        self.assertEqual(0, result['search']['candidateModelCount'])
        reasons = result['selectionDiagnostics']['excludedModels'][0]['reasons']
        self.assertIn('runtime_unknown', reasons)

    def test_small_devices_can_accumulate_into_supported_runtime_range(self):
        self.connection.execute("UPDATE products SET product_type='accessory' WHERE model!='UPS-1'")
        self.curves[0]['points'] = [{'loadW': 300, 'runtimeMinutes': 20}, {'loadW': 700, 'runtimeMinutes': 10}]
        self.curves[0]['batteryConfigurations'][0]['points'] = self.curves[0]['points']
        result = self.optimize(devices=[
            {'name': 'Camera', 'quantity': 8, 'watt': 50, 'va': 62.5, 'priority': 50},
        ])
        self.assertIsNotNone(result['bestSolution'])
        self.assertEqual(1, result['bestSolution']['unitCount'])
        self.assertGreaterEqual(result['bestSolution']['minimumEstimatedRuntimeMinutes'], 10)

    def test_final_load_below_curve_range_remains_invalid(self):
        self.connection.execute("UPDATE products SET product_type='accessory' WHERE model!='UPS-1'")
        self.curves[0]['points'] = [{'loadW': 300, 'runtimeMinutes': 20}, {'loadW': 700, 'runtimeMinutes': 10}]
        self.curves[0]['batteryConfigurations'][0]['points'] = self.curves[0]['points']
        result = self.optimize(devices=[
            {'name': 'Camera', 'quantity': 1, 'watt': 50, 'va': 62.5, 'priority': 50},
        ])
        self.assertIsNone(result['bestSolution'])

    def setUp(self):
        domains = patch.dict(MANUFACTURER_DOMAINS, {'test vendor': ('vendor.example',)})
        domains.start()
        self.addCleanup(domains.stop)
        self.connection = sqlite3.connect(':memory:', isolation_level=None)
        self.catalog = seed_catalog(self.connection)
        self.curves = [verified_curve(
            f'UPS-{index + 1}', 700 + 100 * index, 900 + 100 * index,
            8 if index == 0 else 4 + index,
            [{'loadW': 1, 'runtimeMinutes': 60 + 10 * index},
             {'loadW': 700 + 100 * index, 'runtimeMinutes': 20 + 5 * index}],
        ) for index in range(6)]

    def tearDown(self):
        self.connection.close()

    def optimize(self, devices=None, runtime=10, selected=None):
        if not callable(optimize_from_connection):
            self.fail('optimization_engine.optimize_from_connection is not implemented')
        request = {
            'runtimeRequirementMinutes': runtime,
            'devices': devices or [
                {'name': 'Server', 'quantity': 2, 'watt': 600, 'va': 750, 'priority': 50},
            ],
        }
        if selected is not None:
            request['selectedProductIds'] = selected
        return optimize_from_connection(self.connection, request, curve_catalog=self.curves)

    def test_returns_engineering_valid_best_and_five_alternatives(self):
        result = self.optimize()

        self.assertEqual('CSP', result['solver'])
        self.assertIsNotNone(result['bestSolution'])
        self.assertTrue(result['bestSolution']['validation']['isValid'])
        self.assertEqual(5, len(result['topAlternatives']))
        self.assertTrue(all(row['validation']['isValid'] for row in result['topAlternatives']))
        self.assertLessEqual(result['search']['fleetCandidatesVisited'], result['search']['fleetCandidateBudget'])
        self.assertEqual({'A', 'B', 'C', 'D', 'E'}, {row['code'] for row in result['scenarios']})
        expansion = next(row for row in result['scenarios'] if row['code'] == 'C')
        self.assertTrue(expansion['available'])
        self.assertGreater(expansion['solution']['expansion']['maxBatteryPacks'], 0)
        for unit in result['bestSolution']['units']:
            self.assertLessEqual(unit['usedLoad']['watts'], unit['ratings']['watts'] / 1.2 + 1e-9)
            self.assertLessEqual(unit['usedLoad']['va'], unit['ratings']['va'] / 1.2 + 1e-9)
            self.assertLessEqual(unit['outlets']['used'], unit['outlets']['available'])
            self.assertGreaterEqual(unit['estimatedRuntimeMinutes'], result['bestSolution']['runtimeRequirementMinutes'])

    def test_single_device_search_is_complete_without_enumerating_unused_ups(self):
        result = self.optimize(devices=[
            {'name': 'Server', 'quantity': 1, 'watt': 200, 'va': 250, 'priority': 50},
        ], runtime=5, selected=self.catalog['products'][:2])

        self.assertEqual(1, result['search']['maxUnitsPerDesign'])
        self.assertTrue(result['search']['isComplete'])
        self.assertEqual(2, result['validDesignCount'])
        self.assertEqual(1, len(result['topAlternatives']))

    def test_finds_a_mixed_model_design_when_no_single_model_can_fit_the_load(self):
        # Keep only two verified models: UPS-1 has many outlets but cannot carry
        # the high-watt device; UPS-2 can carry it but has too few outlets for
        # the remaining six devices. A two-unit solution therefore needs both.
        self.connection.execute("UPDATE products SET product_type='accessory' WHERE model NOT IN ('UPS-1','UPS-2')")
        result = self.optimize(devices=[
            {'name': 'Storage', 'quantity': 1, 'watt': 600, 'va': 750, 'priority': 50},
            {'name': 'Cameras', 'quantity': 6, 'watt': 80, 'va': 100, 'priority': 50},
        ])

        best = result['bestSolution']
        self.assertEqual(2, best['unitCount'])
        self.assertEqual({'UPS-1', 'UPS-2'}, {row['model'] for row in best['models']})
        self.assertTrue(best['validation']['capacityValid'])
        self.assertTrue(best['validation']['outletValid'])
        self.assertTrue(best['validation']['runtimeValid'])

    def test_backtracking_finds_a_three_unit_packing_missed_by_best_fit(self):
        self.connection.execute("UPDATE products SET product_type='accessory' WHERE model!='UPS-1'")
        self.connection.execute('''UPDATE product_specifications SET rating_w=12,rating_va=15
            WHERE product_source_id IN (
                SELECT ps.id FROM product_sources ps JOIN products p ON p.id=ps.product_id
                WHERE p.model='UPS-1'
            )''')
        self.curves[0]['ratedWatts'] = 12
        self.curves[0]['ratedVa'] = 15
        self.curves[0]['points'] = [{'loadW': 1, 'runtimeMinutes': 60},
                                    {'loadW': 12, 'runtimeMinutes': 20}]
        self.curves[0]['batteryConfigurations'][0]['points'] = self.curves[0]['points']
        result = self.optimize(devices=[
            {'name': f'Load {watt}', 'quantity': 1, 'watt': watt, 'va': watt / 0.8, 'priority': 50}
            for watt in (9, 6, 5, 3, 2, 2, 2, 1)
        ], runtime=5)

        self.assertEqual(3, result['bestSolution']['unitCount'])
        self.assertTrue(result['search']['isComplete'])
        self.assertEqual(8, sum(row['outlets']['used'] for row in result['bestSolution']['units']))

    def test_zero_load_rows_still_reserve_their_outlets(self):
        result = self.optimize(devices=[
            {'name': 'Server', 'quantity': 1, 'watt': 200, 'va': 250, 'priority': 50},
            {'name': 'No-load appliance', 'quantity': 2, 'watt': 0, 'va': 0, 'priority': 50},
        ])

        self.assertIsNotNone(result['bestSolution'])
        self.assertEqual(3, result['bestSolution']['units'][0]['outlets']['used'])

    def test_zero_load_outlets_can_use_a_second_zero_load_ups(self):
        self.connection.execute("UPDATE products SET product_type='accessory' WHERE model!='UPS-1'")
        result = self.optimize(devices=[
            {'name': 'Server', 'quantity': 1, 'watt': 200, 'va': 250, 'priority': 50},
            {'name': 'No-load appliance', 'quantity': 8, 'watt': 0, 'va': 0, 'priority': 50},
        ])

        best = result['bestSolution']
        self.assertEqual(2, best['unitCount'])
        self.assertEqual(9, sum(row['outlets']['used'] for row in best['units']))
        self.assertEqual(1, sum(row['runtimeStatus'] == 'not_applicable_zero_load' for row in best['units']))
        self.assertTrue(best['validation']['runtimeValid'])
        self.assertTrue(result['search']['isComplete'])

    def test_lowest_cost_is_unavailable_when_vat_is_not_comparable(self):
        self.connection.execute("UPDATE price_offers SET vat_treatment='unknown'")
        result = self.optimize()
        cheapest = next(row for row in result['scenarios'] if row['code'] == 'A')

        self.assertFalse(cheapest['available'])
        self.assertEqual('incomparable_price_basis', cheapest['reasonCode'])
        self.assertEqual('missing_vat', result['priceComparison']['reasonCode'])

    def test_missing_currency_uses_user_confirmed_thb_when_vat_is_comparable(self):
        self.connection.execute("UPDATE price_offers SET vat_treatment='exclusive'")
        result = self.optimize()
        cheapest = next(row for row in result['scenarios'] if row['code'] == 'A')

        self.assertTrue(cheapest['available'])
        self.assertEqual('THB', result['priceComparison']['currency'])
        self.assertEqual('user-confirmed', result['priceComparison']['currencySource'])
        self.assertEqual('THB', result['bestSolution']['totalCostCurrency'])

    def test_inclusive_price_with_verified_effective_period_is_normalized_to_exclusive(self):
        self.connection.execute("UPDATE price_offers SET currency='THB',vat_treatment='exclusive'")
        product_id = self.catalog['products'][0]
        source_id = self.connection.execute('''SELECT id FROM product_sources
            WHERE product_id=? AND source_locator=?''',
            (product_id, f'sheet:UPS;row:{product_id}')).fetchone()[0]
        raw_json = json.loads(self.connection.execute(
            'SELECT raw_json FROM product_sources WHERE id=?', (source_id,)
        ).fetchone()[0])
        raw_json.update(price_effective_from='2026-05-01', price_effective_to='2026-05-31')
        self.connection.execute('UPDATE product_sources SET raw_json=? WHERE id=?',
                                 (json.dumps(raw_json), source_id))
        self.connection.execute('''UPDATE price_offers
            SET amount_decimal='9630',vat_treatment='inclusive',source_date=NULL
            WHERE product_source_id=?''', (source_id,))
        result = self.optimize(devices=[
            {'name': 'Server', 'quantity': 1, 'watt': 200, 'va': 250, 'priority': 50},
        ], runtime=5)
        cheapest = next(row for row in result['scenarios'] if row['code'] == 'A')

        self.assertTrue(cheapest['available'])
        self.assertEqual('exclusive', result['priceComparison'].get('vatBasis'))
        self.assertEqual(1, result['priceComparison'].get('inclusiveOffersNormalized'))
        self.assertEqual(7, result['priceComparison'].get('vatRatePercent'))
        self.assertEqual([{'startsOn': '2026-05-01', 'endsOn': '2026-05-31'}],
                         result['priceComparison']['priceEffectivePeriods'])
        self.assertEqual(9000, cheapest['solution']['totalCost'])
        stored_amount = self.connection.execute(
            'SELECT amount_decimal FROM price_offers WHERE product_source_id=?', (source_id,)
        ).fetchone()[0]
        self.assertEqual('9630', stored_amount)

    def test_inclusive_price_without_verified_effective_period_blocks_comparison(self):
        self.connection.execute("UPDATE price_offers SET currency='THB',vat_treatment='exclusive'")
        product_id = self.catalog['products'][0]
        source_id = self.connection.execute('''SELECT id FROM product_sources
            WHERE product_id=? AND source_locator=?''',
            (product_id, f'sheet:UPS;row:{product_id}')).fetchone()[0]
        for source_date in (None, '2026-05-10', '2024-12-31'):
            with self.subTest(source_date=source_date):
                self.connection.execute('''UPDATE price_offers
                    SET amount_decimal='9630',vat_treatment='inclusive',source_date=?
                    WHERE product_source_id=?''', (source_date, source_id))
                result = self.optimize(devices=[
                    {'name': 'Server', 'quantity': 1, 'watt': 200, 'va': 250, 'priority': 50},
                ], runtime=5)
                cheapest = next(row for row in result['scenarios'] if row['code'] == 'A')

                self.assertFalse(cheapest['available'])
                self.assertEqual('unverified_vat_rate', result['priceComparison']['reasonCode'])

    def test_inclusive_price_with_effective_period_outside_verified_vat_window_blocks(self):
        self.connection.execute("UPDATE price_offers SET currency='THB',vat_treatment='exclusive'")
        product_id = self.catalog['products'][0]
        source_id = self.connection.execute('''SELECT id FROM product_sources
            WHERE product_id=? AND source_locator=?''',
            (product_id, f'sheet:UPS;row:{product_id}')).fetchone()[0]
        raw_json = json.loads(self.connection.execute(
            'SELECT raw_json FROM product_sources WHERE id=?', (source_id,)
        ).fetchone()[0])
        raw_json.update(price_effective_from='2024-12-01', price_effective_to='2024-12-31')
        self.connection.execute('UPDATE product_sources SET raw_json=? WHERE id=?',
                                 (json.dumps(raw_json), source_id))
        self.connection.execute('''UPDATE price_offers
            SET amount_decimal='9630',vat_treatment='inclusive',source_date='2026-05-10'
            WHERE product_source_id=?''', (source_id,))
        result = self.optimize(devices=[
            {'name': 'Server', 'quantity': 1, 'watt': 200, 'va': 250, 'priority': 50},
        ], runtime=5)
        cheapest = next(row for row in result['scenarios'] if row['code'] == 'A')

        self.assertFalse(cheapest['available'])
        self.assertEqual('unverified_vat_rate', result['priceComparison']['reasonCode'])

    def test_explicit_non_thb_currency_blocks_cost_ranking(self):
        self.connection.execute("UPDATE price_offers SET currency='USD',vat_treatment='exclusive'")
        result = self.optimize()
        cheapest = next(row for row in result['scenarios'] if row['code'] == 'A')

        self.assertFalse(cheapest['available'])
        self.assertEqual('non_thb_currency', result['priceComparison']['reasonCode'])

    def test_missing_price_does_not_report_a_comparison_currency(self):
        product_id = self.catalog['products'][0]
        self.connection.execute('''DELETE FROM price_offers WHERE product_source_id IN
            (SELECT id FROM product_sources WHERE product_id=?)''', (product_id,))
        result = self.optimize()

        self.assertFalse(result['priceComparison']['available'])
        self.assertEqual('missing_price', result['priceComparison']['reasonCode'])
        self.assertIsNone(result['priceComparison']['currency'])
        self.assertIsNone(result['priceComparison']['currencySource'])

    def test_lowest_cost_can_rank_when_currency_and_vat_are_comparable(self):
        self.connection.execute("UPDATE price_offers SET currency='THB',vat_treatment='exclusive'")
        result = self.optimize()
        cheapest = next(row for row in result['scenarios'] if row['code'] == 'A')

        self.assertTrue(cheapest['available'])
        self.assertEqual('THB', result['priceComparison']['currency'])
        self.assertIsNotNone(cheapest['solution']['totalCost'])

    def test_one_model_with_multiple_currency_offers_blocks_cost_ranking(self):
        self.connection.execute("UPDATE price_offers SET currency='THB',vat_treatment='exclusive'")
        product_id = self.catalog['products'][0]
        source_id = self.connection.execute('''INSERT INTO product_sources
            (product_id,source_file_id,source_locator,raw_text,raw_json) VALUES (?,?,?,?,?)''', (
            product_id, self.catalog['source_file_id'], 'sheet:UPS;row:extra-price',
            'UPS-1 | Dealer Price | 500',
            '{"manufacturer":"Test Vendor","model":"UPS-1","product_type":"UPS",'
            '"price_type":"Dealer Price","source_path":"SYNTHETIC/catalog.xlsx",'
            '"source_locator":"sheet:UPS;row:extra-price"}',
        )).lastrowid
        self.connection.execute('''INSERT INTO price_offers
            (product_source_id,amount_decimal,currency,vat_treatment,price_type)
            VALUES (?,?,?,?,?)''', (source_id, '500', 'USD', 'inclusive', 'source-marked'))
        result = self.optimize()
        cheapest = next(row for row in result['scenarios'] if row['code'] == 'A')

        self.assertFalse(cheapest['available'])
        self.assertEqual('mixed_currency', result['priceComparison']['reasonCode'])

    def test_runtime_requirement_is_a_hard_constraint(self):
        result = self.optimize(runtime=100)

        self.assertIsNone(result['bestSolution'])
        self.assertEqual([], result['topAlternatives'])

    def test_invalid_input_is_rejected(self):
        with self.assertRaises(OptimizationInputError):
            optimize_from_connection(self.connection, {
                'runtimeRequirementMinutes': 0,
                'devices': [{'name': 'Server', 'quantity': 1, 'watt': 100, 'va': 125, 'priority': 50}],
            }, curve_catalog=self.curves)


if __name__ == '__main__':
    unittest.main()
