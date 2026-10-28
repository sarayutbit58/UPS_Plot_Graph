"""Source-backed evidence must reach the real catalog API without prices leaking."""
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from catalog_search import search_catalog_from_connection
from published_catalog import SCHEMA, project_model
from test_verified_evidence import product, record


class VerifiedCatalogIntegrationTests(unittest.TestCase):
    def test_curated_oem_evidence_reaches_catalog_checks(self):
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.executescript(SCHEMA)
        item = product()
        item['warnings'] = []
        projected = project_model(item, include_prices=True)
        connection.execute('INSERT INTO published_models VALUES(?,?,?,?,?,?,?,?)',
                           (1, 'APC', 'UPS-1', 'UPS-1', 800, 1000, 9, json.dumps(projected)))
        with patch('recommendation_engine._read_verified_evidence_records', create=True,
                   return_value=[record()]):
            result = search_catalog_from_connection(connection, {
                'requiredWatts': 600, 'requiredVa': 700, 'requiredOutlets': 2,
                'runtimeRequirementMinutes': 5, 'headroomPercent': 0,
            }, curve_catalog=[])
        self.assertEqual(1, result['total'])
        model = result['models'][0]
        self.assertTrue(model['identityVerified'])
        self.assertEqual({'watts': 900.0, 'va': 1000.0}, model['ratings'])
        self.assertTrue(model['hasSourceConflicts'])
        self.assertEqual(4, model['batteryBackupOutletCount'])
        self.assertEqual('https://www.se.com/specification/ups-1',
                         model.get('technicalEvidence', {}).get('ratingWatts', {}).get('url'))
        self.assertEqual([('IEC_C13', 'battery', 4), ('IEC_C19', 'surge', 1)],
                         [(item['type'], item['power'], item['count'])
                          for item in model.get('outletGroups', [])])
        self.assertEqual('internal', model['batteryConfigurations'][0]['id'])
        self.assertNotIn('private/', json.dumps(result))

    def test_distributor_crosswalk_is_traceable_without_exposing_private_path(self):
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.executescript(SCHEMA)
        projected = project_model(product(sku='DIST-1'), include_prices=True)
        source_id = projected['evidence'][0]['sourceId']
        candidate = record(sku='DIST-1')
        candidate['crosswalkSource'] = {'sourceId': source_id,
                                        'manufacturerUrl': 'https://www.se.com/crosswalk.pdf'}
        connection.execute('INSERT INTO published_models VALUES(?,?,?,?,?,?,?,?)',
                           (1, 'APC', 'UPS-1', 'DIST-1', 800, 1000, 9, json.dumps(projected)))
        with patch('recommendation_engine._read_verified_evidence_records', create=True,
                   return_value=[candidate]):
            result = search_catalog_from_connection(connection, {
                'requiredWatts': 600, 'requiredVa': 700, 'requiredOutlets': 2,
                'runtimeRequirementMinutes': 5, 'headroomPercent': 0,
            }, curve_catalog=[])
        crosswalk = result['models'][0]['technicalEvidence'].get('identityCrosswalk')
        self.assertEqual(source_id, crosswalk.get('sourceId') if crosswalk else None)
        self.assertNotIn('private/', json.dumps(result))

    def test_unverified_merged_bundle_does_not_show_a_definitive_model_price(self):
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.executescript(SCHEMA)
        item = product()
        item['warnings'].append('Combined a model-only catalogue row with its exact manufacturer-SKU row')
        projected = project_model(item, include_prices=True)
        connection.execute('INSERT INTO published_models VALUES(?,?,?,?,?,?,?,?)',
                           (1, 'APC', 'UPS-1', 'UPS-1', 800, 1000, 9, json.dumps(projected)))
        with patch('recommendation_engine._read_verified_evidence_records', create=True,
                   return_value=[record()]):
            result = search_catalog_from_connection(connection, {
                'query': 'UPS-1', 'mode': 'nearby', 'includeUnknown': True,
                'requiredWatts': 600, 'requiredVa': 700, 'requiredOutlets': 2,
                'runtimeRequirementMinutes': 5, 'headroomPercent': 0,
            }, curve_catalog=[])
        self.assertEqual(1, result['total'])
        self.assertIsNone(result['models'][0]['price'])
        self.assertEqual('bundle_identity_unverified', result['models'][0]['priceUnavailableReason'])

    def test_regional_lifecycle_warning_reaches_catalog_without_claiming_thai_stock(self):
        connection = sqlite3.connect(':memory:')
        self.addCleanup(connection.close)
        connection.executescript(SCHEMA)
        projected = project_model(product(), include_prices=True)
        connection.execute('INSERT INTO published_models VALUES(?,?,?,?,?,?,?,?)',
                           (1, 'APC', 'UPS-1', 'UPS-1', 800, 1000, 9, json.dumps(projected)))
        candidate = record()
        candidate['lifecycleObservation'] = {
            'country': 'Singapore', 'endOfSaleDate': '2023-03-03', 'observedOn': '2026-10-28',
            'thailandStatus': 'unknown', 'source': {'url': 'https://www.se.com/sg/product/ups-1'}}
        with patch('recommendation_engine._read_verified_evidence_records', create=True,
                   return_value=[candidate]):
            result = search_catalog_from_connection(connection, {
                'requiredWatts': 600, 'requiredVa': 700, 'requiredOutlets': 2,
                'runtimeRequirementMinutes': 5, 'headroomPercent': 0,
            }, curve_catalog=[])
        observations = result['models'][0].get('lifecycleObservations') or []
        self.assertEqual('Singapore', observations[0]['country'] if observations else None)
        self.assertEqual('unknown', observations[0]['thailandStatus'])


if __name__ == '__main__':
    unittest.main()
