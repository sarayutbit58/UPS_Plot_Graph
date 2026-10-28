"""Synthetic contracts for staging a private catalog without publishing it."""

from contextlib import closing
import importlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from database.published_catalog import SCHEMA

try:
    stage_module = importlib.import_module('database.stage_verified_catalog')
except ModuleNotFoundError as error:
    if error.name != 'database.stage_verified_catalog':
        raise
    stage_module = None


def source_model(identifier, manufacturer, sku, rating_w, rating_va, *, evidence_id):
    """Build one projected JSON record with distinct synthetic commercial fields."""
    offer = {'amount': '123.4500', 'currency': 'THB', 'vatTreatment': 'exclusive',
             'kind': 'Dealer', 'unknownFutureField': {'revision': 3},
             'source': {'sourceId': evidence_id, 'path': 'source:' + evidence_id}}
    return {'id': identifier, 'manufacturer': manufacturer, 'model': sku, 'sku': sku,
            'manufacturer_id': 1, 'product_type': 'UPS', 'description': None,
            'external_battery_supported': None, 'external_battery_model': None,
            'max_external_battery_packs': None, 'runtime_curve': None,
            'rating_w': rating_w, 'rating_va': rating_va,
            'outlet_count': 4, 'non_ups_reason': None,
            'prices': [offer], 'estimated_cost': dict(offer),
            'evidence': [{'sourceId': evidence_id, 'path': 'source:' + evidence_id,
                          'ratingVaEvidence': True, 'ratingWEvidence': True}],
            'warnings': [], 'custom_projected_field': {'kept': ['unchanged']}}


def maker_record(manufacturer='APC', sku='UPS-1', *, category='UPS'):
    domain = 'www.se.com' if manufacturer == 'APC' else 'www.cyberpower.com'
    url = 'https://' + domain + '/product/' + sku
    return {'manufacturer': manufacturer, 'catalogSku': sku, 'manufacturerSku': sku,
            'category': category, 'identitySource': {'url': url},
            'ratingWatts': 900, 'ratingVa': 1000, 'ratingSource': {'url': url},
            'outletGroups': [{'type': 'IEC_C13', 'power': 'battery', 'count': 4,
                              'source': {'url': url}}],
            'batteryConfigurations': [{'id': 'internal', 'label': 'Internal',
                'points': [{'loadW': 450, 'runtimeMinutes': 10},
                           {'loadW': 900, 'runtimeMinutes': 4}],
                'source': {'url': url}, 'includedPacks': [], 'extraPacks': [],
                'conditions': 'manufacturer estimate'}],
            'conflicts': [{'field': 'outlets', 'outletType': 'IEC_C19',
                           'valueA': 'one', 'valueB': 'two',
                           'sourceA': {'url': url, 'page': 3},
                           'sourceB': {'url': url, 'page': 4}}]}


def write_snapshot(path, *, version='ups-curated-v1', declared_count='3'):
    a = source_model(1, 'APC', 'UPS-1', 800, 1000, evidence_id='a' * 24)
    b = source_model(2, 'CyberPower', 'CL-1', 900, 1000, evidence_id='b' * 24)
    e = source_model(3, 'Eaton', 'ETN-UPS-3', 700, 1000, evidence_id='c' * 24)
    e['evidence'].append({'sourceId': 'd' * 24, 'path': 'source:' + 'd' * 24})
    e.update(identity_verified=True, manufacturer_sku='UPS-3',
             identity_source={'url': 'https://www.eaton.com/product/UPS-3'},
             identity_crosswalk={'sourceId': 'c' * 24,
                                'manufacturerUrl': 'https://www.eaton.com/spec/UPS-3'},
             manufacturer_conflicts=[{'field': 'runtime', 'configurationId': 'previous',
                 'valueA': '5', 'valueB': '4',
                 'sourceA': {'url': 'https://www.eaton.com/a', 'page': 2},
                 'sourceB': {'url': 'https://www.eaton.com/b'}}])
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(SCHEMA)
        for item in (a, b, e):
            connection.execute('INSERT INTO published_models VALUES(?,?,?,?,?,?,?,?)',
                (item['id'], item['manufacturer'], item['model'], item['sku'],
                 item['rating_w'], item['rating_va'], item['outlet_count'],
                 json.dumps(item, ensure_ascii=False)))
        for key, value in [('format', version), ('model_count', declared_count),
                           ('generated_at', '2020-01-01T00:00:00+00:00'),
                           ('includes_prices', 'true'), ('custom_marker', 'preserve-me')]:
            if value is not None:
                connection.execute('INSERT INTO catalog_meta VALUES(?,?)', (key, value))
    return (a, b, e)


def read_snapshot(path):
    with closing(sqlite3.connect(path)) as connection:
        meta = dict(connection.execute('SELECT key,value FROM catalog_meta'))
        rows = connection.execute('SELECT product_id,manufacturer,model,sku,rating_w,rating_va,'
                                  'outlet_count,model_json FROM published_models ORDER BY product_id').fetchall()
    return meta, rows


class StageVerifiedCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source.sqlite3'
        self.output = self.root / 'staged.sqlite3'

    def stage(self, records):
        if stage_module is None:
            self.fail('stage_catalog is not implemented')
        return stage_module.stage_catalog(self.source, self.output, records)

    def test_stage_applies_exact_facts_and_preserves_private_values_and_all_products(self):
        original = write_snapshot(self.source)
        before_bytes = self.source.read_bytes()
        avr = maker_record('CyberPower', 'CL-1', category='voltage_regulator')
        result = self.stage([maker_record(), avr])
        self.assertEqual(self.source.read_bytes(), before_bytes)
        metadata, rows = read_snapshot(self.output)
        self.assertEqual((metadata['format'], metadata['model_count']), ('ups-curated-v2', '3'))
        self.assertNotEqual(metadata['generated_at'], '2020-01-01T00:00:00+00:00')
        self.assertEqual((metadata['includes_prices'], metadata['custom_marker']), ('true', 'preserve-me'))
        self.assertEqual([row[:4] for row in rows],
                         [(1, 'APC', 'UPS-1', 'UPS-1'), (2, 'CyberPower', 'CL-1', 'CL-1'),
                          (3, 'Eaton', 'ETN-UPS-3', 'ETN-UPS-3')])
        models = [json.loads(row[-1]) for row in rows]
        for old, new in zip(original, models):
            self.assertEqual(new['prices'], old['prices'])
            self.assertEqual(new['estimated_cost'], old['estimated_cost'])
            self.assertEqual(new['evidence'], old['evidence'])
            self.assertEqual(new['custom_projected_field'], old['custom_projected_field'])
        self.assertEqual((rows[0][4], models[0]['rating_w']), (900, 900))
        self.assertTrue(models[0]['identity_verified'])
        self.assertEqual(models[1]['non_ups_reason'], 'manufacturer_verified_non_ups')
        self.assertEqual(models[1]['product_category'], 'voltage_regulator')
        self.assertEqual(models[2]['identity_crosswalk'], original[2]['identity_crosswalk'])
        self.assertEqual(models[2]['manufacturer_conflicts'], original[2]['manufacturer_conflicts'])
        conflict = models[0]['manufacturer_conflicts'][0]
        self.assertEqual((conflict['outletType'], conflict['valueA'], conflict['valueB']),
                         ('IEC_C19', 'one', 'two'))
        self.assertEqual((conflict['sourceA']['page'], conflict['sourceB']['page']), (3, 4))
        self.assertEqual(result['models'], 3)
        self.assertEqual(result['matchedRecords'], 2)
        self.assertEqual(result['filename'], self.output.name)
        self.assertNotIn('123.4500', json.dumps(result))

    def test_v2_preserves_old_crosswalk_in_history_when_new_verified_source_is_applied(self):
        original = write_snapshot(self.source, version='ups-curated-v2')
        record = {'manufacturer': 'Eaton', 'catalogSku': 'ETN-UPS-3', 'manufacturerSku': 'UPS-3',
                  'category': 'UPS', 'identitySource': {'url': 'https://www.eaton.com/product/UPS-3'},
                  'crosswalkSource': {'sourceId': 'd' * 24,
                                      'manufacturerUrl': 'https://www.eaton.com/spec/new'},
                  'ratingWatts': 700, 'ratingVa': 1000,
                  'ratingSource': {'url': 'https://www.eaton.com/product/UPS-3'},
                  'outletGroups': [], 'batteryConfigurations': [], 'conflicts': []}
        self.stage([record])
        _, rows = read_snapshot(self.output)
        item = json.loads(rows[2][-1])
        self.assertEqual(item['identity_crosswalk']['sourceId'], 'd' * 24)
        self.assertIn(original[2]['identity_crosswalk'], item['previous_identity_crosswalk'])
        self.assertEqual(item['evidence'], original[2]['evidence'])
        self.assertEqual(item['manufacturer_conflicts'], original[2]['manufacturer_conflicts'])

    def test_existing_output_and_source_path_are_never_overwritten(self):
        write_snapshot(self.source)
        self.output.write_bytes(b'keep existing output')
        with self.assertRaises((ValueError, FileExistsError)):
            self.stage([maker_record()])
        self.assertEqual(self.output.read_bytes(), b'keep existing output')
        before = self.source.read_bytes()
        if stage_module is None:
            self.fail('stage_catalog is not implemented')
        with self.assertRaises((ValueError, FileExistsError)):
            stage_module.stage_catalog(self.source, self.source, [])
        self.assertEqual(self.source.read_bytes(), before)

    def test_occupied_output_path_is_rejected_even_if_its_target_is_missing(self):
        write_snapshot(self.source)
        if stage_module is None:
            self.fail('stage_catalog is not implemented')
        # Windows may not permit creating a real symlink without elevation.
        # Simulate the filesystem's occupied-path result for a dangling link.
        real_lexists = os.path.lexists
        def occupied(path):
            return Path(path) == self.output or real_lexists(path)
        with patch.object(stage_module.os.path, 'lexists', side_effect=occupied):
            with self.assertRaises(FileExistsError):
                self.stage([maker_record()])
        self.assertFalse(self.output.exists())

    def test_unsupported_format_or_bad_model_count_cannot_create_output(self):
        for version, count in [('unknown-format', '3'), ('ups-curated-v1', None),
                               ('ups-curated-v1', '4'), ('ups-curated-v2', '3.0')]:
            with self.subTest(version=version, count=count):
                if self.source.exists():
                    self.source.unlink()
                write_snapshot(self.source, version=version, declared_count=count)
                with self.assertRaises(ValueError):
                    self.stage([maker_record()])
                self.assertFalse(self.output.exists())

    def test_duplicate_or_unmatched_evidence_and_bad_crosswalk_are_rejected(self):
        write_snapshot(self.source)
        other = maker_record(sku='DOES-NOT-EXIST')
        invalid = {'manufacturer': 'Eaton', 'catalogSku': 'ETN-UPS-3', 'manufacturerSku': 'UPS-3',
                   'category': 'UPS', 'identitySource': {'url': 'https://www.eaton.com/product/UPS-3'},
                   'crosswalkSource': {'sourceId': 'f' * 24,
                                       'manufacturerUrl': 'https://www.eaton.com/spec/UPS-3'}}
        for records in ([maker_record(), maker_record()], [other], [invalid]):
            with self.subTest(records=len(records)):
                with self.assertRaises(ValueError):
                    self.stage(records)
                self.assertFalse(self.output.exists())

    def test_nested_price_field_is_rejected_before_output_exists(self):
        write_snapshot(self.source)
        record = maker_record()
        record['additionalEvidence'] = {'observations': [{'dealerPrice': 'synthetic-secret'}]}
        with self.assertRaises(ValueError):
            self.stage([record])
        self.assertFalse(self.output.exists())

    def test_sensitive_keys_are_rejected_at_any_depth(self):
        write_snapshot(self.source)
        for key in ('price', 'estimatedCost', 'amountDecimal', 'accessToken', 'clientSecret',
                    'password', 'pwd', 'passphrase', 'userEmail', 'apiKey', 'authorization'):
            with self.subTest(key=key):
                try:
                    record = maker_record()
                    record['unconsumed'] = [{'nested': {key: 'synthetic-private'}}]
                    with self.assertRaises(ValueError):
                        self.stage([record])
                    self.assertFalse(self.output.exists())
                finally:
                    self.output.unlink(missing_ok=True)

    def test_private_path_in_conflict_or_runtime_conditions_is_rejected(self):
        write_snapshot(self.source)
        for index, (location, private_value) in enumerate([
                ('conflict', 'C:/Users/person/Documents/private.xlsx'),
                ('conditions', r'C:\Users\person\Documents\private.xlsx'),
                ('conditions', '/home/person/private/catalog.sqlite3'),
                ('conditions', r'\\server\private\dealer.xlsx'),
                ('conditions', r'%USERPROFILE%\Documents\catalog.sqlite3'),
                ('conditions', 'private/customer/catalog.sqlite3')]):
            with self.subTest(location=location, case=index):
                try:
                    record = maker_record()
                    if location == 'conflict':
                        record['conflicts'][0]['valueA'] = private_value
                    else:
                        record['batteryConfigurations'][0]['conditions'] = private_value
                    with self.assertRaises(ValueError):
                        self.stage([record])
                    self.assertFalse(self.output.exists())
                finally:
                    self.output.unlink(missing_ok=True)

    def test_credentials_query_email_and_token_values_are_rejected(self):
        write_snapshot(self.source)
        for index, value in enumerate(('https://name:pass@www.se.com/spec',
                      'https://www.se.com/spec?token=synthetic',
                      'www.se.com/spec?version=1',
                      'person@example.com', 'token=synthetic-private',
                      'Bearer synthetic-private-credential')):
            with self.subTest(case=index):
                try:
                    record = maker_record()
                    record['batteryConfigurations'][0]['conditions'] = value
                    with self.assertRaises(ValueError):
                        self.stage([record])
                    self.assertFalse(self.output.exists())
                finally:
                    self.output.unlink(missing_ok=True)

    def test_safe_capacity_text_and_opaque_source_id_are_allowed(self):
        write_snapshot(self.source)
        record = maker_record()
        record['technicalNotes'] = {'capacity': 900, 'sourceId': 'a' * 24,
                                    'conditions': 'Rated capacity 900 W at 230 V',
                                    'reference': 'https://www.se.com/docs/product.pdf'}
        self.stage([record])
        self.assertTrue(self.output.exists())

    def test_mismatched_relational_identity_is_rejected_before_output(self):
        write_snapshot(self.source)
        with closing(sqlite3.connect(self.source)) as connection, connection:
            connection.execute("UPDATE published_models SET sku='DIFFERENT' WHERE product_id=1")
        with self.assertRaises(ValueError):
            self.stage([maker_record()])
        self.assertFalse(self.output.exists())

    def test_unrelated_source_table_is_not_silently_discarded(self):
        write_snapshot(self.source)
        with closing(sqlite3.connect(self.source)) as connection, connection:
            connection.execute('CREATE TABLE unrelated_data(id INTEGER PRIMARY KEY)')
        with self.assertRaises(ValueError):
            self.stage([maker_record()])
        self.assertFalse(self.output.exists())

    def test_failure_during_output_creation_cleans_only_the_created_file(self):
        write_snapshot(self.source)
        if stage_module is None:
            self.fail('stage_catalog is not implemented')
        real_connect = sqlite3.connect
        def fail_output(path, *args, **kwargs):
            if 'mode=ro' in str(path):
                return real_connect(path, *args, **kwargs)
            raise sqlite3.OperationalError('synthetic write failure')
        with patch.object(stage_module.sqlite3, 'connect', side_effect=fail_output):
            with self.assertRaises(sqlite3.OperationalError):
                self.stage([maker_record()])
        self.assertFalse(self.output.exists())
        self.assertTrue(self.source.exists())

    def test_failure_does_not_remove_a_file_replaced_by_another_process(self):
        write_snapshot(self.source)
        if stage_module is None:
            self.fail('stage_catalog is not implemented')
        intruder = self.root / 'other-file'
        intruder.write_bytes(b'created by another process')
        real_connect = sqlite3.connect
        def replace_output(path, *args, **kwargs):
            if 'mode=ro' in str(path):
                return real_connect(path, *args, **kwargs)
            os.replace(intruder, self.output)
            raise sqlite3.OperationalError('synthetic concurrent replacement')
        with patch.object(stage_module.sqlite3, 'connect', side_effect=replace_output):
            with self.assertRaises(sqlite3.OperationalError):
                self.stage([maker_record()])
        self.assertEqual(self.output.read_bytes(), b'created by another process')

    def test_cli_reads_versioned_manifest_without_printing_private_values(self):
        write_snapshot(self.source)
        manifest = self.root / 'manifest.json'
        manifest.write_text(json.dumps({'version': 1, 'records': [maker_record()]}), encoding='utf8')
        script = Path(__file__).resolve().parents[1] / 'stage_verified_catalog.py'
        proc = subprocess.run([sys.executable, str(script), '--source', str(self.source),
                               '--output', str(self.output), '--manifest', str(manifest)],
                              text=True, capture_output=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn('123.4500', proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stdout)['filename'], self.output.name)

    def test_cli_rejects_manifest_with_wrong_version(self):
        write_snapshot(self.source)
        manifest = self.root / 'manifest.json'
        manifest.write_text(json.dumps({'version': 2, 'records': [maker_record()]}), encoding='utf8')
        script = Path(__file__).resolve().parents[1] / 'stage_verified_catalog.py'
        proc = subprocess.run([sys.executable, str(script), '--source', str(self.source),
                               '--output', str(self.output), '--manifest', str(manifest)],
                              text=True, capture_output=True, check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(self.output.exists())

    def test_cli_does_not_log_rejected_manifest_secret(self):
        write_snapshot(self.source)
        manifest = self.root / 'manifest.json'
        record = maker_record()
        record['additionalEvidence'] = {'nested': [{'password': 'synthetic-do-not-log'}]}
        manifest.write_text(json.dumps({'version': 1, 'records': [record]}), encoding='utf8')
        script = Path(__file__).resolve().parents[1] / 'stage_verified_catalog.py'
        proc = subprocess.run([sys.executable, str(script), '--source', str(self.source),
                               '--output', str(self.output), '--manifest', str(manifest)],
                              text=True, capture_output=True, check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn('synthetic-do-not-log', proc.stdout + proc.stderr)
        self.assertFalse(self.output.exists())

    def test_cli_rejects_sensitive_ignored_root_metadata(self):
        write_snapshot(self.source)
        manifest = self.root / 'manifest.json'
        manifest.write_text(json.dumps({'version': 1, 'records': [maker_record()],
                                        'metadata': {'details': [{'partnerCost': 'synthetic-private'}]}}),
                            encoding='utf8')
        script = Path(__file__).resolve().parents[1] / 'stage_verified_catalog.py'
        proc = subprocess.run([sys.executable, str(script), '--source', str(self.source),
                               '--output', str(self.output), '--manifest', str(manifest)],
                              text=True, capture_output=True, check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn('synthetic-private', proc.stdout + proc.stderr)
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
