"""Contracts for adding source-backed products to a new private snapshot."""

from contextlib import closing, redirect_stderr, redirect_stdout
from hashlib import sha256
import importlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from database.published_catalog import SCHEMA

try:
    appender = importlib.import_module('database.append_verified_catalog')
except ModuleNotFoundError as error:
    if error.name != 'database.append_verified_catalog':
        raise
    appender = None


PRIVATE_PATH = 'C:/private-fixture/catalog.xlsx'
SYNTHETIC_PRICE = '87654.321'


def maker_record(sku, *, manufacturer_sku=None, category='UPS', rating_w=900, rating_va=1000,
                 manufacturer='APC'):
    manufacturer_sku = manufacturer_sku or sku
    domain = 'www.cyberpower.com' if manufacturer == 'CyberPower' else 'www.se.com'
    url = 'https://' + domain + '/product/' + manufacturer_sku
    value = {'manufacturer': manufacturer, 'catalogSku': sku, 'manufacturerSku': manufacturer_sku,
             'category': category, 'identitySource': {'url': url}, 'ratingSource': {'url': url},
             'outletGroups': [], 'batteryConfigurations': [], 'conflicts': []}
    if rating_w is not None:
        value['ratingWatts'] = rating_w
    if rating_va is not None:
        value['ratingVa'] = rating_va
    return value


def write_snapshot(path, *, existing_id=1, sku='OLD-1'):
    model = {'id': existing_id, 'manufacturer_id': 1, 'manufacturer': 'APC', 'model': sku,
             'sku': sku, 'product_type': 'UPS', 'rating_w': 800, 'rating_va': 1000,
             'description': None, 'external_battery_supported': None,
             'external_battery_model': None, 'max_external_battery_packs': None,
             'runtime_curve': None, 'warnings': [],
             'outlet_count': None, 'non_ups_reason': None, 'manufacturer_sku': sku,
             'prices': [{'amount': '1234.5', 'kind': 'Dealer', 'vatTreatment': 'unknown',
                         'source': {'sourceId': 'a' * 24, 'path': 'source:' + 'a' * 24}}],
             'estimated_cost': None, 'evidence': [{'sourceId': 'b' * 24}],
             'custom_field': {'keep': 'unchanged'}}
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript(SCHEMA)
        db.execute('INSERT INTO published_models VALUES(?,?,?,?,?,?,?,?)',
                   (existing_id, 'APC', sku, sku, 800, 1000, None, json.dumps(model, indent=2)))
        db.executemany('INSERT INTO catalog_meta VALUES(?,?)', [
            ('format', 'ups-curated-v2'), ('model_count', '1'), ('generated_at', 'old'),
            ('includes_prices', 'true'), ('original_model_count', '560'),
            ('omitted_missing_capacity_count', '559'), ('custom_marker', 'keep')])


def write_raw(path):
    schema = Path(__file__).resolve().parents[1] / 'schema.sql'
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript(schema.read_text(encoding='utf-8'))
        db.execute("INSERT INTO manufacturers VALUES (1, 'APC', 'apc')")
        db.execute("INSERT INTO manufacturers VALUES (2, 'CyberPower', 'cyberpower')")
        db.execute("INSERT INTO currencies VALUES ('THB', 'Thai baht')")
        db.execute('INSERT INTO source_files(id,relative_path,extension,size_bytes,status,scanned_at) '
                   "VALUES(1, ?, '.xlsx', 1, 'imported', '2026-01-01')", (PRIVATE_PATH,))


def add_raw(path, identifier, sku, *, model=None, price_type='Dealer', include_offer=True,
            description=None, vat='unknown', manufacturer_id=1):
    model = model if model is not None else sku
    locator = 'sheet:UPS;row:' + str(identifier)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('INSERT INTO products(id,manufacturer_id,identity_key,model,sku,description,product_type) '
                   "VALUES(?,?,?,?,?,?, 'UPS')", (identifier, manufacturer_id,
                                                     'identity-' + str(identifier), model, sku, description))
        db.execute('INSERT INTO product_sources(id,product_id,source_file_id,source_locator,raw_text,raw_json) '
                   'VALUES(?,?,1,?,?,?)', (identifier, identifier, locator, model,
                                           json.dumps({'price_type': price_type})))
        if include_offer:
            db.execute('INSERT INTO price_offers(product_source_id,amount_decimal,currency,vat_treatment,price_type) '
                       "VALUES(?,?,'THB',?,'source-marked')", (identifier, SYNTHETIC_PRICE, vat))


def snapshot_rows(path):
    with closing(sqlite3.connect(path)) as db:
        return (dict(db.execute('SELECT key,value FROM catalog_meta')),
                db.execute('SELECT * FROM published_models ORDER BY product_id').fetchall())


def crosswalk(identifier, url):
    evidence = {'path': PRIVATE_PATH, 'locator': 'sheet:UPS;row:' + str(identifier),
                'ratingVaEvidence': False, 'ratingWEvidence': False, 'runtimeEvidence': False,
                'outletEvidence': False, 'expansionEvidence': False}
    source_id = sha256(json.dumps(evidence, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
    return {'sourceId': source_id, 'manufacturerUrl': url}


class AppendVerifiedCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.snapshot = self.root / 'existing.sqlite3'
        self.raw = self.root / 'raw.sqlite3'
        self.output = self.root / 'new.sqlite3'
        write_snapshot(self.snapshot)
        write_raw(self.raw)

    def append(self, records):
        if appender is None:
            self.fail('append_catalog is not implemented')
        return appender.append_catalog(self.snapshot, self.raw, self.output, records)

    def test_adds_one_verified_priced_product_without_changing_existing_rows_or_sources(self):
        add_raw(self.raw, 2, 'NEW-1')
        original_snapshot, original_raw = self.snapshot.read_bytes(), self.raw.read_bytes()
        old_meta, old_rows = snapshot_rows(self.snapshot)
        result = self.append([maker_record('NEW-1')])
        self.assertEqual((result['added'], result['models']), (1, 2))
        self.assertEqual(self.snapshot.read_bytes(), original_snapshot)
        self.assertEqual(self.raw.read_bytes(), original_raw)
        meta, rows = snapshot_rows(self.output)
        self.assertEqual(rows[0], old_rows[0])
        model = json.loads(rows[1][-1])
        self.assertEqual((model['id'], model['identity_verified'], model['rating_w_verified'],
                          model['rating_va_verified']), (2, True, True, True))
        self.assertEqual((model['prices'][0]['kind'], model['prices'][0]['vatTreatment']),
                         ('Dealer', 'unknown'))
        self.assertNotIn(PRIVATE_PATH, rows[1][-1])
        self.assertNotIn(SYNTHETIC_PRICE, json.dumps(result))
        self.assertNotIn(PRIVATE_PATH, json.dumps(result))
        self.assertEqual(meta['model_count'], '2')
        self.assertEqual(meta['custom_marker'], old_meta['custom_marker'])
        self.assertNotIn('omitted_missing_capacity_count', meta)
        self.assertEqual(meta['historical_omitted_missing_capacity_count'], '559')

    def test_skips_absent_offer_and_raw_offer_that_is_not_dealer_or_ndp(self):
        add_raw(self.raw, 2, 'NO-OFFER', include_offer=False)
        add_raw(self.raw, 3, 'RETAIL-1', price_type='List Price')
        result = self.append([maker_record('NO-OFFER'), maker_record('RETAIL-1')])
        self.assertEqual(result['added'], 0)
        self.assertEqual(result['skipped']['no_projected_price'], 2)
        self.assertEqual(len(snapshot_rows(self.output)[1]), 1)

    def test_missing_verified_capacity_and_non_ups_are_not_appended(self):
        add_raw(self.raw, 2, 'MISSING-W')
        add_raw(self.raw, 3, 'NON-UPS')
        result = self.append([maker_record('MISSING-W', rating_w=None),
                              maker_record('NON-UPS', category='voltage_regulator')])
        self.assertEqual(result['added'], 0)
        self.assertEqual(sum(result['skipped'].values()), 2)
        self.assertEqual(len(snapshot_rows(self.output)[1]), 1)

    def test_existing_product_id_sku_and_model_alias_are_skipped(self):
        add_raw(self.raw, 1, 'NEW-ID')
        add_raw(self.raw, 3, 'OLD-1')
        add_raw(self.raw, 4, None, model='OLD-1')
        alias = maker_record('OLD-1')
        alias['catalogSku'] = None
        result = self.append([maker_record('NEW-ID'), maker_record('OLD-1'), alias])
        self.assertEqual(result['added'], 0)
        self.assertEqual(sum(result['skipped'].values()), 3)
        self.assertEqual(len(snapshot_rows(self.output)[1]), 1)

    def test_source_model_matching_existing_sku_is_not_appended_under_another_sku(self):
        add_raw(self.raw, 2, 'OTHER-1', model='OLD-1')
        result = self.append([maker_record('OTHER-1')])
        self.assertEqual(result['added'], 0)
        self.assertEqual(result['skipped']['already_published_or_alias'], 1)

    def test_identical_sku_under_a_different_manufacturer_is_added(self):
        add_raw(self.raw, 2, 'OLD-1', manufacturer_id=2)
        result = self.append([maker_record('OLD-1', manufacturer='CyberPower')])
        self.assertEqual((result['added'], result['models']), (1, 2))
        self.assertEqual(json.loads(snapshot_rows(self.output)[1][1][-1])['manufacturer'], 'CyberPower')

    def test_same_sku_for_two_new_manufacturers_does_not_collide(self):
        add_raw(self.raw, 2, 'SHARED-1')
        add_raw(self.raw, 3, 'SHARED-1', manufacturer_id=2)
        result = self.append([maker_record('SHARED-1'),
                              maker_record('SHARED-1', manufacturer='CyberPower')])
        self.assertEqual((result['added'], result['models']), (2, 3))

    def test_distinct_skus_with_the_same_generic_capacity_model_are_not_aliases(self):
        generic = '1000VA/800W'
        with closing(sqlite3.connect(self.snapshot)) as db, db:
            row = db.execute('SELECT model_json FROM published_models WHERE product_id=1').fetchone()
            product = json.loads(row[0])
            product['model'] = generic
            db.execute('UPDATE published_models SET model=?,model_json=? WHERE product_id=1',
                       (generic, json.dumps(product)))
        add_raw(self.raw, 2, 'NEW-1', model=generic)
        result = self.append([maker_record('NEW-1')])
        self.assertEqual((result['added'], result['models']), (1, 2))

    def test_existing_model_only_equal_to_candidate_sku_is_still_an_alias(self):
        with closing(sqlite3.connect(self.snapshot)) as db, db:
            product = json.loads(db.execute(
                'SELECT model_json FROM published_models WHERE product_id=1').fetchone()[0])
            product['sku'] = None
            product['manufacturer_sku'] = None
            db.execute('UPDATE published_models SET sku=NULL,model_json=? WHERE product_id=1',
                       (json.dumps(product),))
        add_raw(self.raw, 2, 'OLD-1')
        result = self.append([maker_record('OLD-1')])
        self.assertEqual(result['added'], 0)
        self.assertEqual(result['skipped']['already_published_or_alias'], 1)

    def test_model_only_generic_name_does_not_block_distinct_exact_sku(self):
        generic = '1000VA/800W'
        with closing(sqlite3.connect(self.snapshot)) as db, db:
            product = json.loads(db.execute(
                'SELECT model_json FROM published_models WHERE product_id=1').fetchone()[0])
            product.update(sku=None, model=generic, manufacturer_sku=None)
            db.execute('UPDATE published_models SET sku=NULL,model=?,model_json=? WHERE product_id=1',
                       (generic, json.dumps(product)))
        add_raw(self.raw, 2, 'NEW-1', model=generic)
        result = self.append([maker_record('NEW-1')])
        self.assertEqual((result['added'], result['models']), (1, 2))

    def test_bundle_with_existing_manufacturer_sku_is_not_auto_merged(self):
        add_raw(self.raw, 2, 'BUNDLE-1')
        record = maker_record('BUNDLE-1', manufacturer_sku='OLD-1')
        record['crosswalkSource'] = crosswalk(2, record['identitySource']['url'])
        result = self.append([record])
        self.assertEqual(result['added'], 0)
        self.assertEqual(result['skipped']['already_published_or_alias'], 1)

    def test_repeated_evidence_and_identity_collision_do_not_select_an_arbitrary_winner(self):
        add_raw(self.raw, 2, 'SAME-1')
        record = maker_record('SAME-1')
        result = self.append([record, dict(record)])
        self.assertEqual(result['added'], 0)
        self.assertEqual(result['skipped']['duplicate_request_identity'], 2)

    def test_raw_battery_accessory_stays_excluded_despite_ups_manifest(self):
        add_raw(self.raw, 2, 'PART-1', description='replacement battery pack')
        result = self.append([maker_record('PART-1')])
        self.assertEqual(result['added'], 0)
        self.assertEqual(result['skipped']['not_verified_ups'], 1)

    def test_alias_offer_is_not_merged_into_the_sku_product(self):
        add_raw(self.raw, 2, 'ALIAS-1', price_type='List Price')
        add_raw(self.raw, 3, None, model='ALIAS-1', price_type='Dealer')
        result = self.append([maker_record('ALIAS-1')])
        self.assertEqual(result['added'], 0)
        self.assertEqual(result['skipped']['no_projected_price'], 1)

    def test_ambiguous_source_identity_is_not_guessed(self):
        add_raw(self.raw, 2, 'DUPLICATE-1')
        add_raw(self.raw, 3, 'DUPLICATE-1')
        result = self.append([maker_record('DUPLICATE-1')])
        self.assertEqual(result['added'], 0)
        self.assertEqual(result['skipped']['ambiguous_source_identity'], 1)

    def test_crosswalk_requires_an_exact_evidence_id_and_does_not_merge_existing_bundle(self):
        add_raw(self.raw, 2, 'DISTRIBUTOR-1')
        record = maker_record('DISTRIBUTOR-1', manufacturer_sku='OEM-1')
        result = self.append([record])
        self.assertEqual(result['added'], 0)
        self.assertEqual(sum(result['skipped'].values()), 1)
        self.output.unlink()
        record['crosswalkSource'] = crosswalk(2, record['identitySource']['url'])
        result = self.append([record])
        self.assertEqual(result['added'], 1)
        product = json.loads(snapshot_rows(self.output)[1][1][-1])
        self.assertEqual(product['manufacturer_sku'], 'OEM-1')
        self.assertNotIn(PRIVATE_PATH, json.dumps(product))

    def test_existing_output_is_never_overwritten_and_errors_do_not_leak(self):
        add_raw(self.raw, 2, 'NEW-1')
        self.output.write_bytes(b'previous output')
        captured_out, captured_err = io.StringIO(), io.StringIO()
        with redirect_stdout(captured_out), redirect_stderr(captured_err):
            with self.assertRaises((ValueError, FileExistsError)) as raised:
                self.append([maker_record('NEW-1')])
        self.assertEqual(self.output.read_bytes(), b'previous output')
        text = str(raised.exception) + captured_out.getvalue() + captured_err.getvalue()
        self.assertNotIn(PRIVATE_PATH, text)
        self.assertNotIn(SYNTHETIC_PRICE, text)

    def test_manifest_private_path_is_rejected_without_creating_output(self):
        add_raw(self.raw, 2, 'NEW-1')
        record = maker_record('NEW-1')
        record['researchNotes'] = [PRIVATE_PATH]
        with self.assertRaises(ValueError) as raised:
            self.append([record])
        self.assertFalse(self.output.exists())
        self.assertNotIn(PRIVATE_PATH, str(raised.exception))

    def test_snapshot_column_change_between_reads_is_rejected(self):
        if appender is None:
            self.fail('append_catalog is not implemented')
        add_raw(self.raw, 2, 'NEW-1')
        read_snapshot = appender._read_snapshot
        columns = {'product_id': 8, 'manufacturer': 'Eaton', 'model': 'CHANGED',
                   'sku': 'CHANGED', 'rating_w': 777, 'rating_va': 999, 'outlet_count': 4}
        for column, replacement in columns.items():
            with self.subTest(column=column):
                snapshot = self.root / ('race-' + column + '.sqlite3')
                output = self.root / ('race-output-' + column + '.sqlite3')
                write_snapshot(snapshot)

                def mutate_after_validation(path, **kwargs):
                    validated = read_snapshot(path, **kwargs)
                    with closing(sqlite3.connect(path)) as db, db:
                        db.execute('UPDATE published_models SET ' + column + '=? WHERE product_id=1',
                                   (replacement,))
                    return validated

                with patch.object(appender, '_read_snapshot', side_effect=mutate_after_validation):
                    with self.assertRaises(ValueError):
                        appender.append_catalog(snapshot, self.raw, output, [maker_record('NEW-1')])
                self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
