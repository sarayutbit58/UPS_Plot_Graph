"""Runtime configurations fail closed when their evidence is malformed."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recommendation_engine import (_verified_runtime_curves, _verified_battery_outlet_count,
                                   _apply_manufacturer_curves, _verified_identity, _verified_rating)


def sample_config():
    return {'id': 'internal', 'label': 'Internal', 'manufacturer_verified': True,
            'points': [{'loadW': 100, 'runtimeMinutes': 20}, {'loadW': 700, 'runtimeMinutes': 5}],
            'source': {'url': 'https://www.se.com/runtime'}, 'includedPacks': [], 'extraPacks': []}


def sample_product(configs):
    return {'manufacturer': 'APC', 'rating_w': 700, 'rating_w_verified': True,
            'rating_w_source': {'url': 'https://www.se.com/spec'}, 'battery_configurations': configs}


class VerifiedRuntimeContractTests(unittest.TestCase):
    def test_unselectable_config_id_is_not_exposed_as_verified(self):
        config = {**sample_config(), 'id': 'invalid id'}
        self.assertEqual([], _verified_runtime_curves(sample_product([config])))

    def test_curve_above_verified_rated_watts_is_not_used(self):
        config = {**sample_config(), 'points': [
            {'loadW': 100, 'runtimeMinutes': 20}, {'loadW': 800, 'runtimeMinutes': 5}]}
        self.assertEqual([], _verified_runtime_curves(sample_product([config])))

    def test_duplicate_configuration_ids_do_not_select_either_curve(self):
        self.assertEqual([], _verified_runtime_curves(sample_product([sample_config(), sample_config()])))

    def test_non_string_configuration_ids_fail_closed(self):
        for identifier in ([], {}, True, 123):
            with self.subTest(identifier=identifier):
                try:
                    result = _verified_runtime_curves(sample_product([{**sample_config(), 'id': identifier}]))
                except Exception as error:
                    self.fail(f'invalid identifier raised {type(error).__name__}')
                self.assertEqual([], result)

    def test_missing_manufacturer_does_not_certify_outlets_or_crash(self):
        product = {'manufacturer': None, 'outlet_groups': [{
            'type': 'IEC_C13', 'power': 'battery', 'count': 4, 'manufacturer_verified': True,
            'source': {'url': 'https://www.se.com/spec'},
        }]}
        try:
            result = _verified_battery_outlet_count(product)
        except Exception as error:
            self.fail(f'missing manufacturer raised {type(error).__name__}')
        self.assertIsNone(result)

    def test_duplicate_outlet_groups_are_not_double_counted(self):
        group = {'type': 'IEC_C13', 'power': 'battery', 'count': 4, 'manufacturer_verified': True,
                 'source': {'url': 'https://www.se.com/spec'}}
        product = {'manufacturer': 'APC', 'outlet_groups': [group, dict(group)]}
        self.assertIsNone(_verified_battery_outlet_count(product))

    def test_legacy_overlay_cannot_recertify_an_unresolved_identity_conflict(self):
        item = sample_product([sample_config()])
        item.update({'model': 'UPS-1', 'sku': 'UPS-1', 'runtime_curve': None,
                     'rating_va': 900, 'outlet_count': 4, 'warnings': [],
                     'manufacturer_conflicts': [{'field': 'identity'}]})
        entry = {'manufacturer': 'APC', 'model': 'UPS-1', 'sku': 'UPS-1',
                 'identityVerified': True, 'ratingWattsVerified': True, 'ratedWatts': 700,
                 'ratingVaVerified': True, 'ratedVa': 900,
                 'points': [{'loadW': 100, 'runtimeMinutes': 20}, {'loadW': 700, 'runtimeMinutes': 5}],
                 'outletGroups': [{'type': 'IEC_C13', 'power': 'battery', 'count': 4,
                                   'manufacturer_verified': True}],
                 'batteryConfigurations': [sample_config()], 'source': {'url': 'https://www.se.com/spec'}}
        _apply_manufacturer_curves([item], [entry])
        self.assertFalse(_verified_identity(item))
        self.assertIsNone(_verified_rating(item, 'rating_w'))
        self.assertIsNone(_verified_battery_outlet_count(item))
        self.assertEqual([], _verified_runtime_curves(item))

    def test_conflicting_verified_rating_in_legacy_overlay_is_not_last_write_wins(self):
        item = sample_product([sample_config()])
        item.update({'model': 'UPS-1', 'sku': 'UPS-1', 'runtime_curve': None,
                     'rating_va': 900, 'outlet_count': 4, 'warnings': [],
                     'identity_verified': True, 'manufacturer_sku': 'UPS-1',
                     'identity_source': {'url': 'https://www.se.com/old'}})
        entry = {'manufacturer': 'APC', 'model': 'UPS-1', 'sku': 'UPS-1',
                 'identityVerified': True, 'ratingWattsVerified': True, 'ratedWatts': 800,
                 'points': [{'loadW': 100, 'runtimeMinutes': 20}, {'loadW': 700, 'runtimeMinutes': 5}],
                 'source': {'url': 'https://www.se.com/new'}}
        _apply_manufacturer_curves([item], [entry])
        self.assertIsNone(_verified_rating(item, 'rating_w'))
        self.assertTrue(item.get('manufacturer_conflicts'))


if __name__ == '__main__':
    unittest.main()
