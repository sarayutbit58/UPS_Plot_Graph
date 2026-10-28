"""Acceptance cases for engineering UPS load and outlet allocation."""

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
    from allocation_engine import AllocationInputError, allocate_from_connection
except ImportError:
    AllocationInputError = ValueError
    allocate_from_connection = None


class AllocationEngineTests(unittest.TestCase):
    def setUp(self):
        domains = patch.dict(MANUFACTURER_DOMAINS, {'test vendor': ('vendor.example',)})
        domains.start()
        self.addCleanup(domains.stop)
        self.connection = sqlite3.connect(':memory:', isolation_level=None)
        self.catalog = seed_catalog(self.connection)
        self.ups1 = self.catalog['products'][0]
        self.ups2 = self.catalog['products'][1]
        self.curves = [
            verified_curve('UPS-1', 700, 900, 4,
                           [{'loadW': 1, 'runtimeMinutes': 60},
                            {'loadW': 700, 'runtimeMinutes': 20}]),
            verified_curve('UPS-2', 800, 1000, 5,
                           [{'loadW': 1, 'runtimeMinutes': 70},
                            {'loadW': 800, 'runtimeMinutes': 25}]),
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

    def test_total_outlets_without_battery_backup_evidence_cannot_allocate(self):
        # An outlet count alone does not say which sockets have battery backup.
        curve = {**self.curves[0]}
        curve.pop('outletGroups')
        result = self.allocate(self.request(), curve_catalog=[curve])
        self.assertFalse(result['validation']['isValid'])
        self.assertEqual([], result['deviceAllocations'])
        self.assertIn('outlet_unknown', result['unassignedDevices'][0]['reasons'])

    def test_unverified_identity_cannot_allocate_even_with_backed_outlets(self):
        curve = {**self.curves[0], 'identityVerified': False, 'outletGroups': [
            {'type': 'IEC_C13', 'power': 'battery', 'manufacturer_verified': True, 'count': 4},
        ]}
        result = self.allocate(self.request(), curve_catalog=[curve])
        self.assertFalse(result['validation']['isValid'])
        self.assertEqual([], result['deviceAllocations'])
        self.assertIn('identity_unverified', result['unassignedDevices'][0]['reasons'])

    def test_legacy_runtime_without_verified_battery_configuration_cannot_allocate(self):
        curve = {**self.curves[0], 'identityVerified': True, 'outletGroups': [
            {'type': 'IEC_C13', 'power': 'battery', 'manufacturer_verified': True, 'count': 4},
        ]}
        curve.pop('batteryConfigurations')
        result = self.allocate(self.request(), curve_catalog=[curve])
        self.assertFalse(result['validation']['isValid'])
        self.assertEqual([], result['deviceAllocations'])
        self.assertIn('runtime_unknown', result['unassignedDevices'][0]['reasons'])

    def test_same_model_can_use_two_distinct_verified_battery_configurations(self):
        curve = {**self.curves[0], 'identityVerified': True,
                 'ratingWattsVerified': True, 'ratedWatts': 700,
                 'ratingVaVerified': True, 'ratedVa': 900, 'outletGroups': [
            {'type': 'IEC_C13', 'power': 'battery', 'manufacturer_verified': True, 'count': 4},
        ], 'batteryConfigurations': [
            {'id': 'internal', 'manufacturer_verified': True, 'label': 'Internal', 'points': [
                {'loadW': 1, 'runtimeMinutes': 60}, {'loadW': 700, 'runtimeMinutes': 20}],
             'includedPacks': [], 'extraPacks': []},
            {'id': 'plus-one', 'manufacturer_verified': True, 'label': 'Internal plus one pack', 'points': [
                {'loadW': 1, 'runtimeMinutes': 120}, {'loadW': 700, 'runtimeMinutes': 40}],
             'includedPacks': [], 'extraPacks': [{'sku': 'EBM-1', 'quantity': 1}]},
        ]}
        request = self.request(selected_ups={'mode': 'fixed', 'models': [
            {'productId': self.ups1, 'batteryConfigId': 'internal', 'quantity': 1},
            {'productId': self.ups1, 'batteryConfigId': 'plus-one', 'quantity': 1},
        ]}, devices=[{'name': 'Server', 'quantity': 2, 'watt': 400, 'va': 500, 'priority': 1}])
        try:
            result = self.allocate(request, curve_catalog=[curve])
        except AllocationInputError as error:
            self.fail(f'distinct configurations of the same model were rejected: {error}')
        self.assertTrue(result['validation']['isValid'])
        self.assertEqual({'internal', 'plus-one'},
                         {unit['batteryConfigId'] for unit in result['upsUtilization']})
        self.assertEqual(2, len(result['deviceAllocations']))

    def test_two_plugs_of_one_device_stay_on_one_ups_and_use_two_sockets(self):
        curve = {**self.curves[0], 'identityVerified': True,
                 'ratingWattsVerified': True, 'ratedWatts': 700,
                 'ratingVaVerified': True, 'ratedVa': 900, 'outletGroups': [
            {'type': 'IEC_C13', 'power': 'battery', 'manufacturer_verified': True, 'count': 4},
        ], 'batteryConfigurations': [{
            'id': 'internal', 'manufacturer_verified': True, 'label': 'Internal', 'points': [
                {'loadW': 1, 'runtimeMinutes': 60}, {'loadW': 700, 'runtimeMinutes': 20}],
            'includedPacks': [], 'extraPacks': [],
        }]}
        result = self.allocate(self.request(devices=[{
            'name': 'Dual PSU server', 'quantity': 1, 'watt': 400, 'va': 500, 'priority': 1,
            'plugs': [{'connector': 'IEC_C14', 'count': 2}],
        }]), curve_catalog=[curve])
        self.assertTrue(result['validation']['isValid'])
        self.assertEqual(1, len(result['deviceAllocations']))
        self.assertEqual(2, len(result['outletAllocations']))
        self.assertEqual(1, len({row['upsUnitId'] for row in result['outletAllocations']}))
        self.assertEqual({'IEC_C13'}, {row['outletType'] for row in result['outletAllocations']})

    def test_connector_mismatch_and_unknown_type_never_pass_as_verified(self):
        for outlet_type, expected in [('IEC_C13', 'connector_mismatch'), (None, 'connector_unknown')]:
            with self.subTest(outlet_type=outlet_type):
                curve = {**self.curves[0], 'identityVerified': True,
                         'ratingWattsVerified': True, 'ratedWatts': 700,
                         'ratingVaVerified': True, 'ratedVa': 900, 'outletGroups': [
                    {'type': outlet_type, 'power': 'battery', 'manufacturer_verified': True, 'count': 4},
                ], 'batteryConfigurations': [{
                    'id': 'internal', 'manufacturer_verified': True, 'label': 'Internal', 'points': [
                        {'loadW': 1, 'runtimeMinutes': 60}, {'loadW': 700, 'runtimeMinutes': 20}],
                    'includedPacks': [], 'extraPacks': [],
                }]}
                result = self.allocate(self.request(devices=[{
                    'name': 'Server', 'quantity': 1, 'watt': 400, 'va': 500, 'priority': 1,
                    'plugs': [{'connector': 'IEC_C20', 'count': 1}],
                }]), curve_catalog=[curve])
                self.assertFalse(result['validation']['isValid'])
                self.assertIn(expected, result['unassignedDevices'][0]['reasons'])

    def test_unspecified_connector_is_labeled_unchecked(self):
        curve = {**self.curves[0], 'identityVerified': True,
                 'ratingWattsVerified': True, 'ratedWatts': 700,
                 'ratingVaVerified': True, 'ratedVa': 900, 'outletGroups': [
            {'type': 'IEC_C13', 'power': 'battery', 'manufacturer_verified': True, 'count': 4},
        ], 'batteryConfigurations': [{
            'id': 'internal', 'manufacturer_verified': True, 'label': 'Internal', 'points': [
                {'loadW': 1, 'runtimeMinutes': 60}, {'loadW': 700, 'runtimeMinutes': 20}],
            'includedPacks': [], 'extraPacks': [],
        }]}
        result = self.allocate(self.request(), curve_catalog=[curve])
        self.assertTrue(result['validation']['isValid'])
        self.assertEqual('not_checked', result['outletAllocations'][0]['compatibilityStatus'])

    def test_untyped_plug_does_not_consume_socket_needed_by_a_later_typed_plug(self):
        curve = {**self.curves[0], 'identityVerified': True,
                 'ratingWattsVerified': True, 'ratedWatts': 700,
                 'ratingVaVerified': True, 'ratedVa': 900, 'outletGroups': [
            {'type': 'IEC_C13', 'power': 'battery', 'manufacturer_verified': True, 'count': 1},
            {'type': 'IEC_C19', 'power': 'battery', 'manufacturer_verified': True, 'count': 1},
        ], 'batteryConfigurations': [{
            'id': 'internal', 'manufacturer_verified': True, 'label': 'Internal', 'points': [
                {'loadW': 1, 'runtimeMinutes': 60}, {'loadW': 700, 'runtimeMinutes': 20}],
            'includedPacks': [], 'extraPacks': [],
        }]}
        result = self.allocate(self.request(devices=[
            {'name': 'Unspecified', 'quantity': 1, 'watt': 100, 'va': 125, 'priority': 1},
            {'name': 'C14 device', 'quantity': 1, 'watt': 100, 'va': 125, 'priority': 1,
             'plugs': [{'connector': 'IEC_C14', 'count': 1}]},
        ]), curve_catalog=[curve])
        self.assertTrue(result['validation']['isValid'])
        typed = next(row for row in result['outletAllocations'] if row['deviceName'] == 'C14 device')
        self.assertEqual('IEC_C13', typed['outletType'])

    def test_unverified_capacity_cannot_pass_even_if_legacy_values_exist(self):
        curve = {**self.curves[0], 'identityVerified': True, 'outletGroups': [
            {'type': 'IEC_C13', 'power': 'battery', 'manufacturer_verified': True, 'count': 4},
        ], 'batteryConfigurations': [{
            'id': 'internal', 'manufacturer_verified': True, 'label': 'Internal', 'points': [
                {'loadW': 1, 'runtimeMinutes': 60}, {'loadW': 700, 'runtimeMinutes': 20}],
            'includedPacks': [], 'extraPacks': [],
        }]}
        curve.pop('ratingWattsVerified')
        curve.pop('ratingVaVerified')
        result = self.allocate(self.request(), curve_catalog=[curve])
        self.assertFalse(result['validation']['isValid'])
        reasons = result['unassignedDevices'][0]['reasons']
        self.assertIn('watt_capacity_unknown', reasons)
        self.assertIn('va_capacity_unknown', reasons)

    def test_invalid_connector_types_are_rejected_as_input_errors(self):
        for connector in ([], {}, True, 123):
            with self.subTest(connector=connector):
                try:
                    self.allocate(self.request(devices=[{
                        'name': 'Server', 'quantity': 1, 'watt': 100, 'va': 125, 'priority': 1,
                        'plugs': [{'connector': connector, 'count': 1}],
                    }]))
                except Exception as error:
                    self.assertIsInstance(error, AllocationInputError)
                else:
                    self.fail('Invalid connector was accepted')

    def test_unmarked_backup_and_runtime_metadata_cannot_self_certify(self):
        self.connection.execute('''CREATE TABLE published_models
            (product_id INTEGER PRIMARY KEY,manufacturer TEXT,model TEXT,sku TEXT,
             rating_w REAL,rating_va REAL,outlet_count INTEGER,model_json TEXT NOT NULL)''')
        item = {'id': 99, 'manufacturer': 'APC', 'model': 'UPS-99', 'sku': 'UPS-99',
                'identity_verified': True, 'manufacturer_sku': 'UPS-99',
                'identity_source': {'url': 'https://www.se.com/product/ups-99'},
                'rating_w': 700, 'rating_va': 900,
                'rating_w_verified': True, 'rating_va_verified': True,
                'rating_w_source': {'url': 'https://www.se.com/spec/ups-99'},
                'rating_va_source': {'url': 'https://www.se.com/spec/ups-99'},
                'outlet_count': 4,
                'outlet_groups': [{'type': 'IEC_C13', 'power': 'battery', 'count': 4,
                                   'source': {'url': 'https://www.se.com/spec/ups-99'}}],
                'battery_configurations': [{'id': 'internal', 'label': 'Internal',
                    'points': [{'loadW': 1, 'runtimeMinutes': 60}, {'loadW': 700, 'runtimeMinutes': 20}],
                    'source': {'url': 'https://www.se.com/runtime/ups-99'},
                    'includedPacks': [], 'extraPacks': []}],
                'non_ups_reason': None, 'prices': [], 'estimated_cost': None,
                'warnings': [], 'evidence': []}
        self.connection.execute('INSERT INTO published_models VALUES(?,?,?,?,?,?,?,?)',
                                (99, 'APC', 'UPS-99', 'UPS-99', 700, 900, 4, json.dumps(item)))
        result = self.allocate(self.request(selected_ups={
            'mode': 'fixed', 'models': [{'productId': 99, 'quantity': 1}]}), curve_catalog=[])
        self.assertFalse(result['validation']['isValid'])
        reasons = result['unassignedDevices'][0]['reasons']
        self.assertIn('outlet_unknown', reasons)
        self.assertIn('runtime_unknown', reasons)

    def test_missing_extra_battery_list_is_not_assumed_to_be_empty(self):
        curve = {**self.curves[0], 'identityVerified': True,
                 'ratingWattsVerified': True, 'ratedWatts': 700,
                 'ratingVaVerified': True, 'ratedVa': 900, 'outletGroups': [
            {'type': 'IEC_C13', 'power': 'battery', 'manufacturer_verified': True, 'count': 4},
        ], 'batteryConfigurations': [{
            'id': 'external', 'label': 'External battery', 'manufacturer_verified': True,
            'points': [{'loadW': 1, 'runtimeMinutes': 60}, {'loadW': 700, 'runtimeMinutes': 20}],
            'includedPacks': [],
        }]}
        result = self.allocate(self.request(), curve_catalog=[curve])
        self.assertFalse(result['validation']['isValid'])
        self.assertIn('runtime_unknown', result['unassignedDevices'][0]['reasons'])

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
        curve = {**self.curves[0]}
        curve.pop('batteryConfigurations')
        result = self.allocate(self.request(
            devices=[{'name': 'Router', 'quantity': 1, 'watt': 200, 'va': 250, 'priority': 1}],
        ), curve_catalog=[curve])
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
        curve = verified_curve('NO-OUTLETS', 800, 1000, 4,
                               [{'loadW': 1, 'runtimeMinutes': 60},
                                {'loadW': 800, 'runtimeMinutes': 20}])
        curve.pop('outletGroups')
        curve.pop('outletCount')
        result = self.allocate(self.request(
            selected_ups={'mode': 'fixed', 'models': [{'productId': product_id, 'quantity': 1}]},
            devices=[{'name': 'Server', 'quantity': 1, 'watt': 400, 'va': 500, 'priority': 1}],
        ), curve_catalog=[curve])
        self.assertEqual([], result['deviceAllocations'])
        self.assertEqual(['outlet_unknown'], result['unassignedDevices'][0]['reasons'])
        self.assertFalse(result['validation']['outletValid'])

    def test_auto_mode_does_not_call_unknown_runtime_an_overload(self):
        curve = {**self.curves[0]}
        curve.pop('batteryConfigurations')
        result = self.allocate(self.request(
            selected_ups={'mode': 'auto', 'models': [{'productId': self.ups1}]},
            devices=[{'name': 'Router', 'quantity': 1, 'watt': 200, 'va': 250, 'priority': 1}],
        ), curve_catalog=[curve])
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
        curve = verified_curve('BIG-UPS', 1e308, 1e308, 4,
                               [{'loadW': 1, 'runtimeMinutes': 60},
                                {'loadW': 1e308, 'runtimeMinutes': 20}])
        with self.assertRaisesRegex(AllocationInputError, 'finite JSON number range'):
            self.allocate(request, curve_catalog=[curve])


if __name__ == '__main__':
    unittest.main()
