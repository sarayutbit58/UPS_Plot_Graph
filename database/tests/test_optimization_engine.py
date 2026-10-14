"""Acceptance tests for the bounded UPS design optimizer."""

from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_recommendation_engine import seed_catalog

try:
    from optimization_engine import OptimizationInputError, optimize_from_connection
except ImportError:
    OptimizationInputError = ValueError
    optimize_from_connection = None


class OptimizationEngineTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:', isolation_level=None)
        self.catalog = seed_catalog(self.connection)
        self.curves = [
            {
                'manufacturer': 'Test Vendor', 'model': 'UPS-1', 'sku': 'UPS-1',
                'points': [{'loadW': 1, 'runtimeMinutes': 60}, {'loadW': 700, 'runtimeMinutes': 20}],
                'outletCount': 8,
                'source': {'url': 'https://vendor.example/ups-1-runtime.pdf'},
            },
            {
                'manufacturer': 'Test Vendor', 'model': 'UPS-2', 'sku': 'UPS-2',
                'points': [{'loadW': 1, 'runtimeMinutes': 70}, {'loadW': 800, 'runtimeMinutes': 25}],
                'outletCount': 5,
                'source': {'url': 'https://vendor.example/ups-2-runtime.pdf'},
            },
        ]

    def tearDown(self):
        self.connection.close()

    def optimize(self, devices=None, runtime=10):
        if not callable(optimize_from_connection):
            self.fail('optimization_engine.optimize_from_connection is not implemented')
        return optimize_from_connection(self.connection, {
            'runtimeRequirementMinutes': runtime,
            'devices': devices or [
                {'name': 'Server', 'quantity': 2, 'watt': 600, 'va': 750, 'priority': 50},
            ],
        }, curve_catalog=self.curves)

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

    def test_lowest_cost_is_unavailable_when_currency_or_vat_is_not_comparable(self):
        result = self.optimize()
        cheapest = next(row for row in result['scenarios'] if row['code'] == 'A')

        self.assertFalse(cheapest['available'])
        self.assertEqual('incomparable_price_basis', cheapest['reasonCode'])

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
