"""End-to-end optimizer contracts for verified battery configurations."""
import json
from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from optimization_engine import optimize_from_connection
from allocation_engine import allocate_from_connection
from recommendation_engine import recommend_from_connection
from published_catalog import SCHEMA


def model():
    source = {'url': 'https://www.se.com/example'}
    configs = [
        {'id': 'internal', 'label': 'Internal battery', 'manufacturer_verified': True,
         'points': [{'loadW': 1, 'runtimeMinutes': 60}, {'loadW': 700, 'runtimeMinutes': 20}],
         'source': source, 'includedPacks': [], 'extraPacks': []},
        {'id': 'plus-one', 'label': 'One extra pack', 'manufacturer_verified': True,
         'points': [{'loadW': 1, 'runtimeMinutes': 120}, {'loadW': 700, 'runtimeMinutes': 40}],
         'source': source, 'includedPacks': [], 'extraPacks': [{'sku': 'BAT-1', 'quantity': 1}]},
    ]
    return {'id': 1, 'manufacturer': 'APC', 'model': 'UPS-1', 'sku': 'UPS-1',
            'product_type': 'UPS', 'non_ups_reason': None,
            'identity_verified': True, 'manufacturer_sku': 'UPS-1', 'identity_source': source,
            'rating_w': 700, 'rating_va': 900, 'rating_w_verified': True, 'rating_va_verified': True,
            'rating_w_source': source, 'rating_va_source': source, 'outlet_count': 4,
            'outlet_groups': [{'type': 'IEC_C13', 'power': 'battery', 'count': 4,
                               'source': source, 'manufacturer_verified': True}],
            'battery_configurations': configs, 'runtime_curve': None,
            'external_battery_supported': True, 'external_battery_model': 'BAT-1',
            'max_external_battery_packs': 1, 'warnings': [], 'evidence': [],
            'prices': [], 'estimated_cost': None}


class VerifiedOptimizerContractTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.connection.executescript(SCHEMA)
        item = model()
        self.connection.execute('INSERT INTO published_models VALUES(?,?,?,?,?,?,?,?)',
                                (1, 'APC', 'UPS-1', 'UPS-1', 700, 900, 4, json.dumps(item)))

    def tearDown(self):
        self.connection.close()

    def request(self):
        return {'runtimeRequirementMinutes': 10,
                'devices': [{'name': 'Server', 'quantity': 2, 'watt': 400, 'va': 500, 'priority': 50}]}

    def test_same_model_with_two_selected_configurations_remains_distinct(self):
        request = self.request()
        request['selectedConfigurations'] = [
            {'productId': 1, 'batteryConfigId': 'internal'},
            {'productId': 1, 'batteryConfigId': 'plus-one'},
        ]
        result = optimize_from_connection(self.connection, request, curve_catalog=[])
        self.assertEqual(2, result['search']['candidateModelCount'])
        self.assertIsNotNone(result['bestSolution'])
        solutions = [result['bestSolution']] + result['topAlternatives']
        self.assertTrue(any({row['batteryConfigId'] for row in solution['models']} == {'internal', 'plus-one'}
                            for solution in solutions))
        self.assertFalse(result['priceComparison']['available'])

    def test_legacy_product_selection_does_not_guess_between_configurations(self):
        request = self.request()
        request['selectedProductIds'] = [1]
        result = optimize_from_connection(self.connection, request, curve_catalog=[])
        self.assertIsNone(result['bestSolution'])
        self.assertIn('battery_configuration_required', result['selectionDiagnostics']['excludedModels'][0]['reasons'])

    def test_recommendation_does_not_rank_base_price_when_any_configuration_needs_extra_battery(self):
        item = model()
        offer = {'amount': '100.00', 'currency': 'THB', 'vatTreatment': 'exclusive',
                 'kind': 'Dealer', 'sourceDate': None, 'source': {'url': 'https://www.se.com/example'}}
        item['prices'] = [offer]
        item['estimated_cost'] = offer
        self.connection.execute('UPDATE published_models SET model_json=? WHERE product_id=1',
                                (json.dumps(item),))
        result = recommend_from_connection(self.connection, {
            'runtimeRequirementMinutes': 10, 'powerFactor': 0.8,
            'devices': [{'name': 'Server', 'quantity': 1, 'watt': 400, 'va': 500}],
        }, curve_catalog=[])
        candidate = result['recommendations'][0]
        self.assertIsNone(candidate['scores']['price'])
        self.assertTrue(candidate['estimatedResellerCost']['excludesBatteryPacks'])

    def test_recommendation_does_not_score_unknown_or_unconverted_vat(self):
        for treatment in (None, 'unknown', 'inclusive', 'unexpected'):
            with self.subTest(treatment=treatment):
                item = model()
                item['battery_configurations'] = item['battery_configurations'][:1]
                offer = {'amount': '100.00', 'currency': 'THB', 'vatTreatment': treatment,
                         'kind': 'Dealer', 'sourceDate': None, 'source': {'url': 'https://www.se.com/example'}}
                item['prices'] = [offer]
                item['estimated_cost'] = offer
                self.connection.execute('UPDATE published_models SET model_json=? WHERE product_id=1',
                                        (json.dumps(item),))
                result = recommend_from_connection(self.connection, {
                    'runtimeRequirementMinutes': 10, 'powerFactor': 0.8,
                    'devices': [{'name': 'Server', 'quantity': 1, 'watt': 400, 'va': 500}],
                }, curve_catalog=[])
                self.assertIsNone(result['recommendations'][0]['scores']['price'])

    def test_model_only_alias_merged_without_bundle_proof_is_not_recommended(self):
        item = model()
        item['warnings'] = ['Combined a model-only catalogue row with its exact manufacturer-SKU row']
        self.connection.execute('UPDATE published_models SET model_json=? WHERE product_id=1',
                                (json.dumps(item),))
        request = self.request()
        request['selectedConfigurations'] = [{'productId': 1, 'batteryConfigId': 'internal'}]
        result = optimize_from_connection(self.connection, request, curve_catalog=[])
        self.assertIsNone(result['bestSolution'])
        self.assertIn('identity_unverified', result['selectionDiagnostics']['excludedModels'][0]['reasons'])

    def test_unspecified_connectors_are_explicitly_not_checked(self):
        request = self.request()
        request['selectedConfigurations'] = [{'productId': 1, 'batteryConfigId': 'internal'}]
        result = optimize_from_connection(self.connection, request, curve_catalog=[])
        self.assertEqual('not_checked', result['bestSolution']['validation'].get('connectorStatus'))

    def test_specified_connectors_are_reported_verified_only_after_matching(self):
        request = self.request()
        request['selectedConfigurations'] = [{'productId': 1, 'batteryConfigId': 'internal'}]
        request['devices'][0]['plugs'] = [{'connector': 'IEC_C14', 'count': 1}]
        result = optimize_from_connection(self.connection, request, curve_catalog=[])
        self.assertEqual('verified', result['bestSolution']['validation'].get('connectorStatus'))
        sources = result['bestSolution']['units'][0]['technicalEvidence']['ratingWatts']
        self.assertEqual('https://www.se.com/example', sources[0]['url'] if sources else None)

    def test_mixed_typed_and_untyped_plugs_with_a_mismatch_are_not_verified(self):
        request = self.request()
        request['devices'] = [{'name': 'Mixed plug device', 'quantity': 1, 'watt': 100, 'va': 125,
                               'priority': 50, 'plugs': [
                                   {'connector': 'IEC_C20', 'count': 1}, {'connector': None, 'count': 1}]}]
        request['selectedUps'] = {'mode': 'fixed', 'models': [
            {'productId': 1, 'batteryConfigId': 'internal', 'quantity': 1}]}
        result = allocate_from_connection(self.connection, request, curve_catalog=[])
        self.assertFalse(result['validation']['isValid'])
        self.assertEqual('not_verified', result['validation']['connectorStatus'])

    def test_fixed_allocation_keeps_zero_load_outlets_without_inventing_runtime(self):
        request = {'runtimeRequirementMinutes': 10,
                   'selectedUps': {'mode': 'fixed', 'models': [
                       {'productId': 1, 'batteryConfigId': 'internal', 'quantity': 2}]},
                   'devices': [
                       {'name': 'Server', 'quantity': 1, 'watt': 400, 'va': 500, 'priority': 50},
                       {'name': 'No-load socket', 'quantity': 4, 'watt': 0, 'va': 0, 'priority': 10},
                   ]}
        try:
            result = allocate_from_connection(self.connection, request, curve_catalog=[])
        except Exception as error:
            self.fail(f'zero-load socket was rejected: {type(error).__name__}')
        self.assertTrue(result['validation']['isValid'])
        self.assertEqual(5, len(result['outletAllocations']))
        self.assertEqual(1, sum(unit['runtimeStatus'] == 'not_applicable_zero_load'
                                for unit in result['upsUtilization']))

    def test_fixed_allocation_allows_partial_loads_to_accumulate_into_curve_range(self):
        item = model()
        item['battery_configurations'][0]['points'] = [
            {'loadW': 200, 'runtimeMinutes': 40}, {'loadW': 700, 'runtimeMinutes': 20}]
        self.connection.execute('UPDATE published_models SET model_json=? WHERE product_id=1',
                                (json.dumps(item),))
        request = {'runtimeRequirementMinutes': 10,
                   'selectedUps': {'mode': 'fixed', 'models': [
                       {'productId': 1, 'batteryConfigId': 'internal', 'quantity': 1}]},
                   'devices': [{'name': 'Camera', 'quantity': 2, 'watt': 150, 'va': 187.5, 'priority': 50}]}
        result = allocate_from_connection(self.connection, request, curve_catalog=[])
        self.assertTrue(result['validation']['isValid'])
        self.assertEqual(2, len(result['deviceAllocations']))
        self.assertEqual(36, result['upsUtilization'][0]['estimatedRuntimeMinutes'])

    def test_fixed_allocation_does_not_accept_final_load_below_curve_range(self):
        item = model()
        item['battery_configurations'][0]['points'] = [
            {'loadW': 200, 'runtimeMinutes': 40}, {'loadW': 700, 'runtimeMinutes': 20}]
        self.connection.execute('UPDATE published_models SET model_json=? WHERE product_id=1',
                                (json.dumps(item),))
        request = {'runtimeRequirementMinutes': 10,
                   'selectedUps': {'mode': 'fixed', 'models': [
                       {'productId': 1, 'batteryConfigId': 'internal', 'quantity': 1}]},
                   'devices': [{'name': 'Camera', 'quantity': 1, 'watt': 150, 'va': 187.5, 'priority': 50}]}
        result = allocate_from_connection(self.connection, request, curve_catalog=[])
        self.assertFalse(result['validation']['isValid'])
        self.assertEqual([], result['deviceAllocations'])
        self.assertIn('runtime_unknown', result['unassignedDevices'][0]['reasons'])

    def test_fixed_allocation_prefers_a_confirmed_runtime_over_a_provisional_unit(self):
        item = model()
        item['battery_configurations'][0]['points'] = [
            {'loadW': 200, 'runtimeMinutes': 40}, {'loadW': 700, 'runtimeMinutes': 20}]
        item['battery_configurations'][1]['points'] = [
            {'loadW': 50, 'runtimeMinutes': 80}, {'loadW': 700, 'runtimeMinutes': 30}]
        self.connection.execute('UPDATE published_models SET model_json=? WHERE product_id=1',
                                (json.dumps(item),))
        request = {'runtimeRequirementMinutes': 10,
                   'selectedUps': {'mode': 'fixed', 'models': [
                       {'productId': 1, 'batteryConfigId': 'internal', 'quantity': 1},
                       {'productId': 1, 'batteryConfigId': 'plus-one', 'quantity': 1}]},
                   'devices': [{'name': 'Camera', 'quantity': 1, 'watt': 150, 'va': 187.5, 'priority': 50}]}
        result = allocate_from_connection(self.connection, request, curve_catalog=[])
        self.assertTrue(result['validation']['isValid'])
        self.assertEqual('UPS-02', result['deviceAllocations'][0]['upsUnitId'])


if __name__ == '__main__':
    unittest.main()
