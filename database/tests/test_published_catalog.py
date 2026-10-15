"""Synthetic projection checks: facts survive; private source material does not."""

from decimal import Decimal
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from published_catalog import SCHEMA, FORMAT, export_catalog, project_model, load_published_inventory
from publish_turso import publish


def model():
    offer = {'amount': '123.45', 'decimal': Decimal('123.45'), 'currency': 'THB',
             'vatTreatment': 'exclusive', 'kind': 'Dealer', 'sourceDate': None,
             'source': {'path': 'C:/private/customer-name/partner-cost.xlsx', 'locator': 'secret-sheet'}}
    return {'id': 1, 'manufacturer_id': 1, 'manufacturer': 'Synthetic', 'model': 'UPS1',
            'sku': 'UPS1', 'product_type': 'UPS', 'rating_w': 1000, 'rating_va': 1500,
            'outlet_count': 4, 'external_battery_supported': None,
            'external_battery_model': None, 'max_external_battery_packs': None,
            'warnings': ['Source price date is not recorded', 'Conflicting rating values'],
            'prices': [offer], 'estimated_cost': offer, 'non_ups_reason': None,
            'description': 'Private customer description',
            'evidence': [{**offer['source'], 'ratingWEvidence': True}],
            'runtime_curve': {'points': [{'loadW': 500, 'runtimeMinutes': 10},
                                        {'loadW': 1000, 'runtimeMinutes': 4}],
                              'source': {'url': 'https://www.se.com/official.pdf',
                                         'title': 'Official synthetic table'}}}


def write_export(path, row_count=1, declared_count='1'):
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(SCHEMA)
        connection.execute('INSERT INTO catalog_meta VALUES(?,?)', ('format', FORMAT))
        if declared_count is not None:
            connection.execute('INSERT INTO catalog_meta VALUES(?,?)', ('model_count', declared_count))
        for index in range(row_count):
            item = {**project_model(model()), 'id': index + 1}
            connection.execute('INSERT INTO published_models VALUES(?,?,?,?,?,?,?,?)',
                (item['id'], item['manufacturer'], item['model'], item['sku'],
                 item['rating_w'], item['rating_va'], item['outlet_count'], json.dumps(item)))


class FakeRemoteStore:
    def __init__(self):
        self.calls = []

    def query(self, sql):
        self.calls.append('query')
        return []

    def pipeline(self, requests):
        self.calls.append('pipeline')
        steps = requests[0]['batch']['steps']
        return [{'result': {'step_results': [{}] * len(steps),
                             'step_errors': [None] * len(steps)}}]


class PublishedCatalogTests(unittest.TestCase):
    def test_default_omits_prices_paths_raw_description_and_keeps_unknowns(self):
        result = project_model(model())
        text = json.dumps(result)
        for forbidden in ('private', 'customer-name', 'partner-cost', 'secret-sheet', '123.45'):
            self.assertNotIn(forbidden, text)
        self.assertEqual(result['prices'], [])
        self.assertIsNone(result['external_battery_supported'])
        self.assertEqual(result['runtime_curve']['points'][0]['runtimeMinutes'], 10)
        self.assertEqual(result['runtime_curve']['source']['url'], 'https://www.se.com/official.pdf')

    def test_private_price_offer_roundtrip_preserves_distinct_basis(self):
        item = model()
        item['prices'].append({**item['prices'][0], 'vatTreatment': 'inclusive',
                               'priceEffectiveFrom': '2026-01-01'})
        projected = project_model(item, True)
        with closing(sqlite3.connect(':memory:')) as connection, connection:
            connection.executescript(SCHEMA)
            connection.execute('INSERT INTO published_models VALUES(?,?,?,?,?,?,?,?)',
                (1, 'Synthetic', 'UPS1', 'UPS1', 1000, 1500, 4, json.dumps(projected)))
            loaded = load_published_inventory(connection)[0]
        self.assertEqual(len(loaded['prices']), 2)
        self.assertEqual(loaded['prices'][1]['vatTreatment'], 'inclusive')
        self.assertEqual(loaded['estimated_cost']['decimal'], Decimal('123.45'))
        self.assertNotIn('partner-cost', json.dumps(projected))

    def test_export_filters_incomplete_and_accessories_without_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / 'source.db', Path(directory) / 'export.db'
            sqlite3.connect(source).close()
            inventory = [model(), {**model(), 'id': 2, 'rating_w': None},
                         {**model(), 'id': 3, 'non_ups_reason': 'battery accessory'}]
            with patch('recommendation_engine._build_inventory', return_value=inventory), \
                    patch('recommendation_engine._apply_manufacturer_curves'):
                result = export_catalog(source, target)
            self.assertEqual(result['models'], 1)
            self.assertEqual(result['sourceRealUpsCount'], 2)
            self.assertEqual(result['omittedMissingCapacityCount'], 1)
            with closing(sqlite3.connect(target)) as connection:
                metadata = dict(connection.execute('SELECT key,value FROM catalog_meta'))
            self.assertEqual(metadata['original_model_count'], '2')
            self.assertEqual(metadata['omitted_missing_capacity_count'], '1')
            self.assertEqual(metadata['model_count'], '1')
            with self.assertRaises(ValueError):
                export_catalog(source, target)

    def test_include_incomplete_reports_zero_omitted(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / 'source.db', Path(directory) / 'export.db'
            sqlite3.connect(source).close()
            with patch('recommendation_engine._build_inventory', return_value=[
                    model(), {**model(), 'id': 2, 'rating_w': None}]), \
                    patch('recommendation_engine._apply_manufacturer_curves'):
                result = export_catalog(source, target, include_incomplete=True)
            self.assertEqual(result['models'], 2)
            self.assertEqual(result['omittedMissingCapacityCount'], 0)

    def test_publisher_refuses_unrelated_database(self):
        class Store:
            def query(self, sql):
                return [('customer_secrets',)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'export.db'
            write_export(path)
            with self.assertRaisesRegex(ValueError, 'unrelated'):
                publish(path, Store())

    def test_publication_batch_is_transactional_and_has_rollback(self):
        class Store:
            def query(self, sql):
                return []
            def pipeline(self, requests):
                self.steps = requests[0]['batch']['steps']
                return [{'result': {'step_results': [{}] * len(self.steps),
                                     'step_errors': [None] * len(self.steps)}}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'export.db'
            write_export(path)
            store = Store()
            publish(path, store)
            self.assertEqual(store.steps[0]['stmt']['sql'], 'BEGIN IMMEDIATE')
            self.assertEqual(store.steps[-2]['stmt']['sql'], 'COMMIT')
            self.assertEqual(store.steps[-1]['stmt']['sql'], 'ROLLBACK')
            self.assertEqual(store.steps[-1]['condition']['type'], 'not')

    def test_publisher_rejects_empty_export_before_any_remote_call(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'empty.db'
            write_export(path, row_count=0, declared_count='0')
            store = FakeRemoteStore()
            with self.assertRaisesRegex(ValueError, 'empty'):
                publish(path, store)
            self.assertEqual(store.calls, [])

    def test_publisher_rejects_incorrect_model_count_before_any_remote_call(self):
        for declared_count in ('0', '2', '-1', 'not-a-count', '1.0', None):
            with self.subTest(declared_count=declared_count), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'mismatch.db'
                write_export(path, declared_count=declared_count)
                store = FakeRemoteStore()
                with self.assertRaisesRegex(ValueError, 'model_count'):
                    publish(path, store)
                self.assertEqual(store.calls, [])


if __name__ == '__main__':
    unittest.main()
