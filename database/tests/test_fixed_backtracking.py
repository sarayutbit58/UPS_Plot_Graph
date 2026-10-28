"""Fixed-fleet allocation must not discard a feasible provisional placement."""

from contextlib import closing
import json
import sqlite3
import unittest
from unittest.mock import patch

from database import allocation_engine
from database.published_catalog import SCHEMA


def config(identifier, minimum, maximum):
    source = {'url': 'https://www.se.com/test/runtime-' + identifier}
    return {'id': identifier, 'label': 'Synthetic ' + identifier,
            'manufacturer_verified': True, 'source': source,
            'includedPacks': [], 'extraPacks': [],
            'points': [{'loadW': minimum, 'runtimeMinutes': 60},
                       {'loadW': maximum, 'runtimeMinutes': 20}]}


def model(identifier, configurations, groups=None):
    sku = 'SYNTHETIC-' + str(identifier)
    source = {'url': 'https://www.se.com/test/' + sku}
    groups = groups if groups is not None else [('IEC_C13', 4)]
    return {'id': identifier, 'manufacturer_id': 1, 'manufacturer': 'APC',
            'model': sku, 'sku': sku, 'product_type': 'UPS', 'product_category': 'UPS',
            'manufacturer_sku': sku, 'identity_verified': True, 'identity_source': source,
            'rating_w': 900, 'rating_va': 1200,
            'rating_w_verified': True, 'rating_va_verified': True,
            'rating_w_source': source, 'rating_va_source': source,
            'outlet_count': sum(count for _, count in groups),
            'outlet_groups': [{'type': outlet_type, 'power': 'battery', 'count': count,
                               'manufacturer_verified': True, 'source': source}
                              for outlet_type, count in groups],
            'battery_configurations': configurations, 'non_ups_reason': None,
            'prices': [{'amount': '98765.4321', 'currency': 'THB', 'kind': 'Dealer'}],
            'estimated_cost': None, 'warnings': [], 'evidence': []}


def device(name='Server', quantity=2, watt=150, priority=5, plugs=None):
    value = {'name': name, 'quantity': quantity, 'watt': watt, 'va': watt,
             'priority': priority}
    if plugs is not None:
        value['plugs'] = plugs
    return value


class FixedBacktrackingTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.connection.executescript(SCHEMA)
        self.addCleanup(self.connection.close)

    def add(self, product):
        self.connection.execute('INSERT INTO published_models VALUES(?,?,?,?,?,?,?,?)',
            (product['id'], product['manufacturer'], product['model'], product['sku'],
             product['rating_w'], product['rating_va'], product['outlet_count'],
             json.dumps(product)))

    def allocate(self, models, devices):
        return allocation_engine.allocate_from_connection(self.connection, {
            'selectedUps': {'mode': 'fixed', 'models': models},
            'runtimeRequirementMinutes': 10, 'devices': devices,
        }, curve_catalog=[])

    def seed_two(self):
        self.add(model(1, [config('A', 200, 700)]))
        self.add(model(2, [config('B', 50, 200)]))
        return [{'productId': 1, 'batteryConfigId': 'A', 'quantity': 1},
                {'productId': 2, 'batteryConfigId': 'B', 'quantity': 1}]

    def test_combines_two_partial_loads_on_curve_a_after_greedy_rolls_one_back(self):
        result = self.allocate(self.seed_two(), [device()])
        self.assertTrue(result['validation']['isValid'])
        self.assertTrue(result['searchComplete'])
        self.assertEqual([], result['unassignedDevices'])
        self.assertEqual(2, len(result['deviceAllocations']))
        self.assertEqual({1}, {row['productId'] for row in result['deviceAllocations']})
        self.assertEqual(300, result['upsUtilization'][0]['usedLoad']['watts'])
        self.assertGreaterEqual(result['upsUtilization'][0]['estimatedRuntimeMinutes'], 10)
        self.assertNotIn('98765.4321', json.dumps(result))

    def test_final_single_load_below_curve_remains_unassigned(self):
        self.add(model(1, [config('A', 200, 700)]))
        result = self.allocate([{'productId': 1, 'batteryConfigId': 'A', 'quantity': 1}],
                               [device(quantity=1)])
        self.assertFalse(result['validation']['isValid'])
        self.assertTrue(result['searchComplete'])
        self.assertEqual([], result['deviceAllocations'])
        self.assertIn('runtime_unknown', result['unassignedDevices'][0]['reasons'])

    def test_mixed_configs_replan_connectors_and_keep_multi_plug_device_together(self):
        groups = [('IEC_C13', 1), ('IEC_C19', 3)]
        self.add(model(1, [config('A', 200, 700), config('B', 50, 200)], groups))
        units = [{'productId': 1, 'batteryConfigId': 'A', 'quantity': 1},
                 {'productId': 1, 'batteryConfigId': 'B', 'quantity': 1}]
        devices = [device('Untyped dual cord', quantity=1, priority=10,
                          plugs=[{'connector': None, 'count': 2}]),
                   device('Typed dual cord', quantity=1, priority=5,
                          plugs=[{'connector': 'IEC_C14', 'count': 1},
                                 {'connector': 'IEC_C20', 'count': 1}])]
        result = self.allocate(units, devices)
        self.assertTrue(result['validation']['isValid'])
        self.assertEqual(['A', 'B'], [unit['batteryConfigId'] for unit in result['upsUtilization']])
        self.assertEqual(4, len(result['outletAllocations']))
        self.assertEqual({'UPS-01'}, {row['upsUnitId'] for row in result['outletAllocations']})
        typed = [row for row in result['outletAllocations'] if row['deviceName'] == 'Typed dual cord']
        self.assertEqual({'IEC_C14': 'IEC_C13', 'IEC_C20': 'IEC_C19'},
                         {row['connector']: row['outletType'] for row in typed})
        self.assertEqual({'IEC_C19'}, {row['outletType'] for row in result['outletAllocations']
                                      if row['deviceName'] == 'Untyped dual cord'})

    def test_zero_watt_device_is_preserved_when_fallback_reassigns_other_devices(self):
        result = self.allocate(self.seed_two(), [device(), device('Passive device', 1, 0, priority=1)])
        self.assertTrue(result['validation']['isValid'])
        self.assertEqual(3, len(result['deviceAllocations']))
        zero = next(row for row in result['deviceAllocations'] if row['name'] == 'Passive device')
        self.assertEqual((zero['watt'], zero['va'], len(zero['outletNumbers'])), (0, 0, 1))
        self.assertEqual(3, len(result['outletAllocations']))

    def test_node_limit_preserves_greedy_partial_and_marks_search_incomplete(self):
        units = self.seed_two()
        with patch.object(allocation_engine, 'MAX_FIXED_SEARCH_NODES', 1, create=True):
            result = self.allocate(units, [device()])
        self.assertFalse(result['validation']['isValid'])
        self.assertFalse(result['searchComplete'])
        self.assertEqual('fixed_search_node_limit', result['searchReason'])
        self.assertEqual(1, len(result['deviceAllocations']))
        self.assertEqual(1, result['unassignedDevices'][0]['quantity'])
        self.assertIn('runtime_unknown', result['unassignedDevices'][0]['reasons'])

    def test_instance_limit_marks_incomplete_without_losing_greedy_priority(self):
        units = self.seed_two()
        devices = [device('Critical', quantity=1, priority=10),
                   device('Low priority', quantity=1, priority=1)]
        with patch.object(allocation_engine, 'MAX_FIXED_SEARCH_DEVICES', 1, create=True):
            result = self.allocate(units, devices)
        self.assertFalse(result['searchComplete'])
        self.assertEqual('fixed_search_device_limit', result['searchReason'])
        self.assertEqual(['Critical'], [row['name'] for row in result['deviceAllocations']])
        self.assertEqual('Low priority', result['unassignedDevices'][0]['name'])

    def test_unit_and_outlet_limits_report_incomplete_search(self):
        units = self.seed_two()
        for limit_name, expected in [('MAX_FIXED_SEARCH_UNITS', 'fixed_search_unit_limit'),
                                     ('MAX_FIXED_SEARCH_OUTLETS', 'fixed_search_outlet_limit')]:
            with self.subTest(limit=limit_name), patch.object(allocation_engine, limit_name, 1):
                result = self.allocate(units, [device()])
                self.assertFalse(result['searchComplete'])
                self.assertFalse(result['validation']['searchComplete'])
                self.assertEqual(expected, result['searchReason'])
                self.assertEqual(expected, result['validation']['searchReason'])
                self.assertEqual(1, len(result['deviceAllocations']))

    def test_runtime_curve_boundaries_are_not_extrapolated(self):
        self.add(model(1, [config('A', 200, 700)]))
        units = [{'productId': 1, 'batteryConfigId': 'A', 'quantity': 1}]
        for watt, expected_valid in [(199, False), (200, True), (700, True), (701, False)]:
            with self.subTest(watt=watt):
                result = self.allocate(units, [device(quantity=1, watt=watt)])
                self.assertEqual(expected_valid, result['validation']['isValid'])
                if not expected_valid:
                    self.assertEqual([], result['deviceAllocations'])
                    self.assertIn('runtime_unknown', result['unassignedDevices'][0]['reasons'])

    def test_unknown_universal_connector_is_not_assumed_compatible(self):
        self.add(model(1, [config('A', 100, 700)], [('UNIVERSAL', 4)]))
        result = self.allocate([{'productId': 1, 'batteryConfigId': 'A', 'quantity': 1}],
                               [device(quantity=1, plugs=[{'connector': 'IEC_C14', 'count': 1}])])
        self.assertFalse(result['validation']['isValid'])
        self.assertTrue(result['searchComplete'])
        self.assertEqual([], result['deviceAllocations'])
        self.assertIn('connector_unknown', result['unassignedDevices'][0]['reasons'])


if __name__ == '__main__':
    unittest.main()
