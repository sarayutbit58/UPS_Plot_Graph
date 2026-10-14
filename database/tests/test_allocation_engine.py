"""Acceptance cases for engineering UPS load and outlet allocation."""

from pathlib import Path
import json
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_recommendation_engine import seed_catalog

try:
    from allocation_engine import AllocationInputError, allocate_from_connection
except ImportError:
    AllocationInputError = ValueError
    allocate_from_connection = None


class AllocationEngineTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:', isolation_level=None)
        self.catalog = seed_catalog(self.connection)
        self.ups1 = self.catalog['products'][0]
        self.ups2 = self.catalog['products'][1]
        self.curves = [
            {
                'manufacturer': 'Test Vendor', 'model': 'UPS-1', 'sku': 'UPS-1',
                'points': [
                    {'loadW': 1, 'runtimeMinutes': 60},
                    {'loadW': 700, 'runtimeMinutes': 20},
                ],
                'outletCount': 4,
                'source': {'url': 'https://vendor.example/ups-1-runtime.pdf'},
            },
            {
                'manufacturer': 'Test Vendor', 'model': 'UPS-2', 'sku': 'UPS-2',
                'points': [
                    {'loadW': 1, 'runtimeMinutes': 70},
                    {'loadW': 800, 'runtimeMinutes': 25},
                ],
                'outletCount': 5,
                'source': {'url': 'https://vendor.example/ups-2-runtime.pdf'},
            },
        ]

    def tearDown(self):
        self.connection.close()

    def request(self, devices=None, selected_ups=None, runtime=10):
        return {
            'selectedUps': selected_ups or {
                'mode': 'fixed',
                'models': [{'productId': self.ups1, 'quantity': 1}],
            },
            'runtimeRequirementMinutes': runtime,
            'devices': devices or [
                {'name': 'Server', 'quantity': 1, 'watt': 400, 'va': 500, 'priority': 1},
            ],
        }

    def allocate(self, request, curve_catalog=None):
        if not callable(allocate_from_connection):
            self.fail('allocation_engine.allocate_from_connection is not implemented')
        return allocate_from_connection(
            self.connection, request,
            curve_catalog=self.curves if curve_catalog is None else curve_catalog,
        )

    def test_fixed_mode_assigns_every_device_instance_to_a_unit_and_outlet(self):
        result = self.allocate(self.request(
            selected_ups={'mode': 'fixed', 'models': [{'productId': self.ups1, 'quantity': 2}]},
            devices=[{'name': 'Server', 'quantity': 2, 'watt': 400, 'va': 500, 'priority': 5}],
        ))
        self.assertTrue(result['validation']['isValid'])
        self.assertEqual(2, len(result['deviceAllocations']))
        self.assertEqual({'UPS-01', 'UPS-02'}, {row['upsUnitId'] for row in result['deviceAllocations']})
        self.assertEqual(2, len(result['outletAllocations']))
        self.assertEqual([1, 1], [row['outletNumber'] for row in result['outletAllocations']])
        self.assertEqual(2, len(result['upsUtilization']))

    def test_fixed_mode_supports_quantities_for_a_mixed_model_pool(self):
        result = self.allocate(self.request(
            selected_ups={'mode': 'fixed', 'models': [
                {'productId': self.ups1, 'quantity': 1},
                {'productId': self.ups2, 'quantity': 1},
            ]},
            devices=[
                {'name': 'Large server', 'quantity': 1, 'watt': 600, 'va': 700, 'priority': 5},
                {'name': 'Small server', 'quantity': 1, 'watt': 400, 'va': 500, 'priority': 3},
            ],
        ))
        self.assertTrue(result['validation']['isValid'])
        self.assertEqual({'UPS-1', 'UPS-2'}, {row['model'] for row in result['upsUtilization']})
        self.assertTrue(all(not row['autoAdded'] for row in result['upsUtilization']))

    def test_auto_mode_uses_starting_units_before_adding_more(self):
        result = self.allocate(self.request(
            selected_ups={'mode': 'auto', 'models': [{'productId': self.ups1, 'quantity': 1}]},
            devices=[{'name': 'Server', 'quantity': 2, 'watt': 400, 'va': 500, 'priority': 3}],
        ))
        self.assertTrue(result['validation']['isValid'])
        self.assertEqual([False, True], [row['autoAdded'] for row in result['upsUtilization']])

    def test_priority_is_preserved_when_fixed_capacity_cannot_fit_every_device(self):
        result = self.allocate(self.request(devices=[
            {'name': 'Critical server', 'quantity': 1, 'watt': 400, 'va': 500, 'priority': 5},
            {'name': 'Low priority server', 'quantity': 1, 'watt': 200, 'va': 250, 'priority': 1},
        ]))
        self.assertFalse(result['validation']['isValid'])
        self.assertEqual(['Critical server'], [row['name'] for row in result['deviceAllocations']])
        self.assertEqual('Low priority server', result['unassignedDevices'][0]['name'])
        self.assertIn('watt_capacity', result['unassignedDevices'][0]['reasons'])

    def test_reports_outlet_shortage_when_capacity_and_runtime_are_sufficient(self):
        result = self.allocate(self.request(devices=[
            {'name': 'Access point', 'quantity': 5, 'watt': 100, 'va': 125, 'priority': 1},
        ]))
        self.assertEqual(4, len(result['deviceAllocations']))
        self.assertEqual(1, result['unassignedDevices'][0]['quantity'])
        self.assertEqual(['outlet_capacity'], result['unassignedDevices'][0]['reasons'])
        self.assertTrue(result['validation']['capacityValid'])
        self.assertFalse(result['validation']['outletValid'])
        self.assertIn('increase_verified_outlets', [row['code'] for row in result['engineeringRecommendations']])

    def test_reports_watt_shortage_when_outlets_are_sufficient(self):
        result = self.allocate(self.request(devices=[
            {'name': 'PoE switch', 'quantity': 2, 'watt': 300, 'va': 375, 'priority': 1},
        ]))
        self.assertEqual(1, len(result['deviceAllocations']))
        self.assertEqual(1, result['unassignedDevices'][0]['quantity'])
        self.assertIn('watt_capacity', result['unassignedDevices'][0]['reasons'])
        self.assertTrue(result['validation']['outletValid'])
        self.assertFalse(result['validation']['capacityValid'])
        self.assertIn('increase_ups_capacity', [row['code'] for row in result['engineeringRecommendations']])

    def test_runtime_below_requirement_blocks_device_placement(self):
        result = self.allocate(self.request(
            devices=[{'name': 'Storage', 'quantity': 1, 'watt': 500, 'va': 600, 'priority': 1}],
            runtime=35,
        ))
        self.assertEqual([], result['deviceAllocations'])
        self.assertEqual(['runtime_insufficient'], result['unassignedDevices'][0]['reasons'])
        self.assertFalse(result['validation']['runtimeValid'])
        self.assertIn('select_verified_runtime', [row['code'] for row in result['engineeringRecommendations']])

    def test_unknown_runtime_without_curve_blocks_device_placement(self):
        result = self.allocate(self.request(
            devices=[{'name': 'Router', 'quantity': 1, 'watt': 200, 'va': 250, 'priority': 1}],
        ), curve_catalog=[])
        self.assertEqual([], result['deviceAllocations'])
        self.assertEqual(['runtime_unknown'], result['unassignedDevices'][0]['reasons'])
        self.assertFalse(result['validation']['runtimeValid'])

    def test_unknown_outlet_count_blocks_device_placement(self):
        product_id = self.connection.execute('''INSERT INTO products
            (manufacturer_id,identity_key,model,sku,description,product_type)
            SELECT manufacturer_id,'NO-OUTLETS','NO-OUTLETS','NO-OUTLETS','UPS without outlet count','UPS'
            FROM products WHERE id=?''', (self.ups1,)).lastrowid
        source_id = self.connection.execute('''INSERT INTO product_sources
            (product_id,source_file_id,source_locator,raw_text,raw_json) VALUES (?,?,?,?,?)''',
            (product_id, self.catalog['source_file_id'], 'sheet:UPS;row:no-outlets', 'NO-OUTLETS',
             json.dumps({'manufacturer': 'Test Vendor', 'model': 'NO-OUTLETS', 'sku': 'NO-OUTLETS'}))).lastrowid
        self.connection.execute('''INSERT INTO product_specifications
            (product_source_id,rating_va,rating_w) VALUES (?,?,?)''', (source_id, 1000, 800))
        curve = {
            'manufacturer': 'Test Vendor', 'model': 'NO-OUTLETS', 'sku': 'NO-OUTLETS',
            'points': [{'loadW': 1, 'runtimeMinutes': 60}, {'loadW': 800, 'runtimeMinutes': 20}],
            'source': {'url': 'https://vendor.example/no-outlets-runtime.pdf'},
        }
        result = self.allocate(self.request(
            selected_ups={'mode': 'fixed', 'models': [{'productId': product_id, 'quantity': 1}]},
            devices=[{'name': 'Server', 'quantity': 1, 'watt': 400, 'va': 500, 'priority': 1}],
        ), curve_catalog=[curve])
        self.assertEqual([], result['deviceAllocations'])
        self.assertEqual(['outlet_unknown'], result['unassignedDevices'][0]['reasons'])
        self.assertFalse(result['validation']['outletValid'])

    def test_auto_mode_does_not_call_unknown_runtime_an_overload(self):
        result = self.allocate(self.request(
            selected_ups={'mode': 'auto', 'models': [{'productId': self.ups1}]},
            devices=[{'name': 'Router', 'quantity': 1, 'watt': 200, 'va': 250, 'priority': 1}],
        ), curve_catalog=[])
        self.assertEqual([], result['deviceAllocations'])
        self.assertFalse(result['capacityReport']['overloaded'])
        self.assertEqual(['select_verified_runtime'], [row['code'] for row in result['engineeringRecommendations']])

    def test_auto_mode_mixes_selected_models_and_adds_only_when_needed(self):
        result = self.allocate(self.request(
            selected_ups={'mode': 'auto', 'models': [
                {'productId': self.ups1}, {'productId': self.ups2},
            ]},
            devices=[
                {'name': 'Large server', 'quantity': 1, 'watt': 600, 'va': 700, 'priority': 5},
                {'name': 'Small server', 'quantity': 1, 'watt': 400, 'va': 500, 'priority': 3},
            ],
        ))
        self.assertTrue(result['validation']['isValid'])
        self.assertEqual({'UPS-1', 'UPS-2'}, {row['model'] for row in result['upsUtilization']})
        self.assertTrue(all(row['autoAdded'] for row in result['upsUtilization']))

    def test_reports_overload_when_device_exceeds_selected_ups_rating(self):
        result = self.allocate(self.request(devices=[
            {'name': 'High load server', 'quantity': 1, 'watt': 800, 'va': 1000, 'priority': 1},
        ]))
        self.assertFalse(result['validation']['isValid'])
        self.assertTrue(result['capacityReport']['overloaded'])
        self.assertIn('watt_overload', result['unassignedDevices'][0]['reasons'])
        self.assertIn('va_overload', result['unassignedDevices'][0]['reasons'])

    def test_reports_single_device_overload_even_when_combined_ups_capacity_is_enough(self):
        result = self.allocate(self.request(
            selected_ups={'mode': 'fixed', 'models': [{'productId': self.ups1, 'quantity': 2}]},
            devices=[{'name': 'Single server', 'quantity': 1, 'watt': 800, 'va': 800, 'priority': 1}],
        ))
        self.assertFalse(result['validation']['isValid'])
        self.assertGreater(result['capacityReport']['fleetCapacity']['ratedWatts'], 800)
        self.assertTrue(result['capacityReport']['overloadedWatts'])
        self.assertTrue(result['capacityReport']['overloaded'])

    def test_reports_aggregate_overload_when_each_device_fits_individually(self):
        result = self.allocate(self.request(devices=[
            {'name': 'Server A', 'quantity': 1, 'watt': 400, 'va': 500, 'priority': 5},
            {'name': 'Server B', 'quantity': 1, 'watt': 400, 'va': 500, 'priority': 3},
        ]))
        self.assertEqual(1, len(result['deviceAllocations']))
        self.assertEqual(1, result['unassignedDevices'][0]['quantity'])
        self.assertTrue(result['capacityReport']['overloaded'])

    def test_assignment_runtime_matches_final_unit_load(self):
        result = self.allocate(self.request(devices=[
            {'name': 'Device A', 'quantity': 1, 'watt': 300, 'va': 375, 'priority': 2},
            {'name': 'Device B', 'quantity': 1, 'watt': 100, 'va': 125, 'priority': 2},
        ]))
        unit_runtime = result['upsUtilization'][0]['estimatedRuntimeMinutes']
        self.assertEqual(2, len(result['deviceAllocations']))
        self.assertEqual([unit_runtime, unit_runtime], [
            row['estimatedRuntimeMinutes'] for row in result['deviceAllocations']
        ])

    def test_surfaces_capacity_outlet_evidence_and_conflict_warnings(self):
        result = self.allocate(self.request())
        unit = result['upsUtilization'][0]
        self.assertTrue(unit['technicalEvidence']['ratingWatts'])
        self.assertTrue(unit['technicalEvidence']['ratingVa'])
        self.assertTrue(unit['technicalEvidence']['outlets'])
        self.assertTrue(any('Conflicting source rating_va' in warning for warning in unit['sourceWarnings']))
        self.assertFalse(any('price' in warning.casefold() for warning in unit['sourceWarnings']))

    def test_requires_runtime_target_and_device_priority(self):
        missing_runtime = self.request()
        del missing_runtime['runtimeRequirementMinutes']
        with self.assertRaises(AllocationInputError):
            self.allocate(missing_runtime)
        missing_priority = self.request(devices=[{'name': 'Router', 'quantity': 1, 'watt': 50, 'va': 60}])
        with self.assertRaises(AllocationInputError):
            self.allocate(missing_priority)

    def test_rejects_selected_fleet_capacity_outside_finite_json_range(self):
        product_id = self.connection.execute('''INSERT INTO products
            (manufacturer_id,identity_key,model,sku,description,product_type)
            SELECT manufacturer_id,'BIG-UPS','BIG-UPS','BIG-UPS','Big UPS','UPS'
            FROM products WHERE id=?''', (self.ups1,)).lastrowid
        source_file_id = self.catalog['source_file_id']
        raw = {'manufacturer': 'Test Vendor', 'model': 'BIG-UPS', 'sku': 'BIG-UPS'}
        source_id = self.connection.execute('''INSERT INTO product_sources
            (product_id,source_file_id,source_locator,raw_text,raw_json) VALUES (?,?,?,?,?)''',
            (product_id, source_file_id, 'sheet:UPS;row:big-ups', 'BIG-UPS',
             json.dumps(raw))).lastrowid
        self.connection.execute('''INSERT INTO product_specifications
            (product_source_id,rating_va,rating_w) VALUES (?,?,?)''', (source_id, 1e308, 1e308))
        request = self.request(
            selected_ups={'mode': 'fixed', 'models': [{'productId': product_id, 'quantity': 2}]},
            devices=[{'name': 'Small load', 'quantity': 1, 'watt': 100, 'va': 125, 'priority': 1}],
        )
        with self.assertRaisesRegex(AllocationInputError, 'finite JSON number range'):
            self.allocate(request)


if __name__ == '__main__':
    unittest.main()
