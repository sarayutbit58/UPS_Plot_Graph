"""Exercise snapshot guards with actual SQLite transactions, never a network."""

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import publish_turso
from published_catalog import FORMAT, SCHEMA
from turso_store import TursoError


def write_catalog(path, *, version=FORMAT, revision='old', price='100', padding=''):
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(SCHEMA)
        connection.executemany('INSERT INTO catalog_meta VALUES(?,?)', [
            ('format', version), ('model_count', '2'), ('generated_at', revision),
            ('includes_prices', 'true'),
        ])
        for item_id in (1, 2):
            model = json.dumps({'id': item_id, 'manufacturer': 'Synthetic',
                                'model': f'Model-{item_id}', 'sku': f'SKU-{item_id}',
                                'rating_w': 700.0 + item_id, 'rating_va': 900.0 + item_id,
                                'outlet_count': 4, 'revision': revision,
                                'manufacturer_id': 1, 'product_type': 'UPS', 'description': None,
                                'non_ups_reason': None, 'external_battery_supported': None,
                                'external_battery_model': None, 'max_external_battery_packs': None,
                                'warnings': [], 'evidence': [], 'estimated_cost': None,
                                'runtime_curve': None, 'prices': [{'amount': price}], 'padding': padding})
            connection.execute('INSERT INTO published_models VALUES(?,?,?,?,?,?,?,?)', (
                item_id, 'Synthetic', f'Model-{item_id}', f'SKU-{item_id}',
                700.0 + item_id, 900.0 + item_id, 4, model))
        connection.commit()


def digest(connection):
    metadata = connection.execute('SELECT key,value FROM catalog_meta ORDER BY key').fetchall()
    models = connection.execute('SELECT * FROM published_models ORDER BY product_id').fetchall()
    return hashlib.sha256(repr((metadata, models)).encode('utf-8')).digest()


def bind_export_to_source(export, source):
    fingerprint = publish_turso.snapshot_fingerprint(*publish_turso._read_catalog(source, expected=True))
    with closing(sqlite3.connect(export)) as connection, connection:
        connection.execute('INSERT OR REPLACE INTO catalog_meta VALUES(?,?)',
                           (publish_turso.BASE_SNAPSHOT_KEY, fingerprint))


def assert_export_was_published(test, export, remote):
    with closing(sqlite3.connect(export)) as expected:
        metadata = dict(expected.execute('SELECT key,value FROM catalog_meta'))
        metadata.pop(publish_turso.BASE_SNAPSHOT_KEY, None)
        models = expected.execute('SELECT * FROM published_models ORDER BY product_id').fetchall()
    test.assertEqual(metadata, dict(remote.query('SELECT key,value FROM catalog_meta')))
    test.assertEqual(models, remote.query('SELECT * FROM published_models ORDER BY product_id'))


class SqliteRemote:
    """Interpret the publisher's conditional Hrana batch against real SQLite."""
    def __init__(self):
        self.connection = sqlite3.connect(':memory:', isolation_level=None)
        self.pipeline_calls = 0
        self.before_begin = None
        self.last_body_size = None
        self.fail_rollback = False

    def seed(self, path):
        with closing(sqlite3.connect(path)) as source:
            source.backup(self.connection)

    def query(self, sql, args=()):
        return self.connection.execute(sql, args).fetchall()

    @staticmethod
    def _condition(condition, results, errors):
        kind = condition['type']
        if kind == 'ok':
            step = condition['step']
            return results[step] is not None and errors[step] is None
        if kind == 'not':
            return not SqliteRemote._condition(condition['cond'], results, errors)
        raise AssertionError('Unexpected synthetic batch condition')

    @staticmethod
    def _arg(argument):
        kind = argument['type']
        if kind == 'null':
            return None
        if kind == 'integer':
            return int(argument['value'])
        if kind == 'float':
            return float(argument['value'])
        if kind == 'text':
            return argument['value']
        raise AssertionError('Unexpected synthetic argument')

    def pipeline(self, requests):
        self.pipeline_calls += 1
        self.last_body_size = len(json.dumps({'requests': requests + [{'type': 'close'}]},
                                             allow_nan=False).encode())
        steps = requests[0]['batch']['steps']
        results = [None] * len(steps)
        errors = [None] * len(steps)
        for index, step in enumerate(steps):
            if 'condition' in step and not self._condition(step['condition'], results, errors):
                continue
            sql = step['stmt']['sql']
            if sql == 'BEGIN IMMEDIATE' and self.before_begin:
                self.before_begin(self.connection)
            if sql == 'ROLLBACK' and self.fail_rollback:
                errors[index] = {'message': 'synthetic rollback failure'}
                continue
            try:
                args = [self._arg(arg) for arg in step['stmt'].get('args', [])]
                cursor = self.connection.execute(sql, args)
                results[index] = {'affected_row_count': max(0, cursor.rowcount)}
            except sqlite3.Error:
                # Never include statement args or commercial data in error text.
                errors[index] = {'message': 'synthetic SQLite statement failed'}
        return [{'result': {'step_results': results, 'step_errors': errors}}]

    def close(self):
        self.connection.close()


class PublisherConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.expected = root / 'expected.sqlite3'
        self.output = root / 'new.sqlite3'
        write_catalog(self.expected, version='ups-curated-v1')
        write_catalog(self.output, revision='new')
        bind_export_to_source(self.output, self.expected)
        self.remote = SqliteRemote()
        self.addCleanup(self.remote.close)

    def test_initial_publish_to_a_dedicated_empty_database_needs_no_snapshot(self):
        result = publish_turso.publish(self.output, self.remote)
        self.assertEqual(2, result['models'])
        assert_export_was_published(self, self.output, self.remote)

    def test_initial_publish_to_empty_catalog_tables_needs_no_snapshot(self):
        self.remote.connection.executescript(SCHEMA)
        result = publish_turso.publish(self.output, self.remote)
        self.assertEqual(2, result['models'])
        assert_export_was_published(self, self.output, self.remote)

    def test_rows_without_metadata_are_not_treated_as_an_empty_database(self):
        self.remote.connection.executescript(SCHEMA)
        self.remote.connection.execute('INSERT INTO published_models VALUES(?,?,?,?,?,?,?,?)',
                                       (1, 'Synthetic', 'X', 'X', 1, 1, 1, '{}'))
        before = digest(self.remote.connection)
        with self.assertRaises(TursoError):
            publish_turso.publish(self.output, self.remote)
        self.assertEqual(before, digest(self.remote.connection))

    def test_existing_catalog_requires_expected_snapshot_and_remains_unchanged(self):
        self.remote.seed(self.expected)
        before = digest(self.remote.connection)
        with self.assertRaisesRegex(ValueError, 'expected'):
            publish_turso.publish(self.output, self.remote)
        self.assertEqual(before, digest(self.remote.connection))
        self.assertEqual(0, self.remote.pipeline_calls)

    def test_invalid_staged_json_or_mismatched_index_is_rejected_before_publish(self):
        self.remote.seed(self.expected)
        before = digest(self.remote.connection)
        for value in ('{invalid json', '[]', '{"id":1}',
                      '{"id":1,"manufacturer":"Wrong"}'):
            with self.subTest(value=value), closing(sqlite3.connect(self.output)) as connection:
                connection.execute('UPDATE published_models SET model_json=? WHERE product_id=1', (value,))
                connection.commit()
                with self.assertRaises(ValueError):
                    publish_turso.publish(self.output, self.remote, expected_snapshot_path=self.expected)
                self.assertEqual(before, digest(self.remote.connection))
                self.assertEqual(0, self.remote.pipeline_calls)

    def test_missing_existing_row_or_protected_facts_are_rejected_before_publish(self):
        self.remote.seed(self.expected)
        before = digest(self.remote.connection)
        for field in ('prices', 'evidence', 'estimated_cost', 'sku', 'row'):
            with self.subTest(field=field):
                candidate = Path(self.temp.name) / f'loss-{field}.sqlite3'
                with closing(sqlite3.connect(self.output)) as source, closing(sqlite3.connect(candidate)) as target:
                    source.backup(target)
                with closing(sqlite3.connect(candidate)) as connection, connection:
                    if field == 'row':
                        connection.execute('DELETE FROM published_models WHERE product_id=1')
                        connection.execute("UPDATE catalog_meta SET value='1' WHERE key='model_count'")
                    else:
                        model = json.loads(connection.execute(
                            'SELECT model_json FROM published_models WHERE product_id=1').fetchone()[0])
                        if field == 'sku':
                            model['sku'] = 'changed'
                            connection.execute("UPDATE published_models SET sku='changed' WHERE product_id=1")
                        elif field == 'prices':
                            model['prices'] = []
                        elif field == 'evidence':
                            # Use a protected value in both original and staged snapshots below.
                            model['evidence'] = [{'sourceId': 'modified'}]
                        else:
                            model['estimated_cost'] = {'amount': '101'}
                        connection.execute('UPDATE published_models SET model_json=? WHERE product_id=1',
                                           (json.dumps(model),))
                with self.assertRaises(ValueError):
                    publish_turso.publish(candidate, self.remote, expected_snapshot_path=self.expected)
                self.assertEqual(before, digest(self.remote.connection))
                self.assertEqual(0, self.remote.pipeline_calls)

    def test_verified_evidence_and_history_cannot_silently_disappear(self):
        fields = {
            'identity_source': {'url': 'https://manufacturer.example/identity'},
            'rating_w_source': {'url': 'https://manufacturer.example/watts'},
            'rating_va_source': {'url': 'https://manufacturer.example/va'},
            'identity_crosswalk': {'sourceId': 'synthetic'},
            'manufacturer_outlet_source': {'url': 'https://manufacturer.example/outlets'},
            'runtime_curve': {'points': [{'loadW': 100, 'runtimeMinutes': 10}],
                              'source': {'url': 'https://manufacturer.example/curve'}},
            'outlet_groups': [{'type': 'IEC_C13', 'power': 'battery', 'count': 4}],
            'battery_configurations': [{'id': 'internal', 'points': []}],
            'manufacturer_conflicts': [{'field': 'runtime'}],
            'lifecycle_observations': [{'country': 'Thailand'}],
            'previous_rating_w_source': [{'url': 'https://manufacturer.example/old'}],
            'original_source_ratings': {'rating_w': 600},
        }
        for field, value in fields.items():
            with self.subTest(field=field):
                expected = Path(self.temp.name) / f'original-{field}.sqlite3'
                candidate = Path(self.temp.name) / f'missing-{field}.sqlite3'
                write_catalog(expected)
                write_catalog(candidate)
                with closing(sqlite3.connect(expected)) as connection, connection:
                    model = json.loads(connection.execute(
                        'SELECT model_json FROM published_models WHERE product_id=1').fetchone()[0])
                    model[field] = value
                    connection.execute('UPDATE published_models SET model_json=? WHERE product_id=1',
                                       (json.dumps(model),))
                bind_export_to_source(candidate, expected)
                remote = SqliteRemote()
                self.addCleanup(remote.close)
                remote.seed(expected)
                before = digest(remote.connection)
                with self.assertRaises(ValueError):
                    publish_turso.publish(candidate, remote, expected_snapshot_path=expected)
                self.assertEqual(before, digest(remote.connection))
                self.assertEqual(0, remote.pipeline_calls)

    def test_invalid_expansion_count_is_rejected_before_publish(self):
        self.remote.seed(self.expected)
        before = digest(self.remote.connection)
        for invalid in ({'unexpected': 1}, '3', -1, True, 1.5):
            with self.subTest(value=invalid), closing(sqlite3.connect(self.output)) as connection:
                model = json.loads(connection.execute(
                    'SELECT model_json FROM published_models WHERE product_id=1').fetchone()[0])
                model['external_battery_supported'] = True
                model['max_external_battery_packs'] = invalid
                connection.execute('UPDATE published_models SET model_json=? WHERE product_id=1',
                                   (json.dumps(model),))
                connection.commit()
                with self.assertRaises(ValueError):
                    publish_turso.publish(self.output, self.remote, expected_snapshot_path=self.expected)
                self.assertEqual(before, digest(self.remote.connection))
                self.assertEqual(0, self.remote.pipeline_calls)

    def test_invalid_legacy_runtime_curve_is_rejected_before_publish(self):
        self.remote.seed(self.expected)
        before = digest(self.remote.connection)
        for invalid in ('broken', {}, {'points': [], 'source': 'broken'},
                        {'points': 'broken', 'source': {}}):
            with self.subTest(value=invalid), closing(sqlite3.connect(self.output)) as connection:
                model = json.loads(connection.execute(
                    'SELECT model_json FROM published_models WHERE product_id=1').fetchone()[0])
                model['runtime_curve'] = invalid
                connection.execute('UPDATE published_models SET model_json=? WHERE product_id=1',
                                   (json.dumps(model),))
                connection.commit()
                with self.assertRaises(ValueError):
                    publish_turso.publish(self.output, self.remote, expected_snapshot_path=self.expected)
                self.assertEqual(before, digest(self.remote.connection))
                self.assertEqual(0, self.remote.pipeline_calls)

    def test_invalid_lifecycle_and_evidence_reference_types_are_rejected(self):
        for field, value in (('lifecycle_observations', True),
                             ('lifecycle_observations', 3),
                             ('evidence', [{'path': []}]),
                             ('evidence', [{'locator': {}}])):
            with self.subTest(field=field, value=value), closing(sqlite3.connect(self.output)) as connection:
                remote = SqliteRemote()
                self.addCleanup(remote.close)
                model = json.loads(connection.execute(
                    'SELECT model_json FROM published_models WHERE product_id=1').fetchone()[0])
                model.pop('lifecycle_observations', None)
                model['evidence'] = []
                model[field] = value
                connection.execute('UPDATE published_models SET model_json=? WHERE product_id=1',
                                   (json.dumps(model),))
                connection.commit()
                with self.assertRaises(ValueError):
                    publish_turso.publish(self.output, remote)
                self.assertEqual(0, remote.pipeline_calls)

    def test_price_extremes_are_rejected_before_initial_publish(self):
        for amount in ('1e-10000', '1e308', '0', '-10'):
            with self.subTest(amount=amount), closing(sqlite3.connect(self.output)) as connection:
                remote = SqliteRemote()
                self.addCleanup(remote.close)
                model = json.loads(connection.execute(
                    'SELECT model_json FROM published_models WHERE product_id=1').fetchone()[0])
                model['prices'][0]['amount'] = amount
                connection.execute('UPDATE published_models SET model_json=? WHERE product_id=1',
                                   (json.dumps(model),))
                connection.commit()
                with self.assertRaises(ValueError):
                    publish_turso.publish(self.output, remote)
                self.assertEqual(0, remote.pipeline_calls)

    def test_matching_v1_snapshot_allows_upgrade_to_v2(self):
        self.remote.seed(self.expected)
        result = publish_turso.publish(self.output, self.remote, expected_snapshot_path=self.expected)
        self.assertEqual(2, result['models'])
        assert_export_was_published(self, self.output, self.remote)

    def test_matching_v2_snapshot_allows_update(self):
        with closing(sqlite3.connect(self.expected)) as connection:
            connection.execute("UPDATE catalog_meta SET value=? WHERE key='format'", (FORMAT,))
            connection.commit()
        bind_export_to_source(self.output, self.expected)
        self.remote.seed(self.expected)
        publish_turso.publish(self.output, self.remote, expected_snapshot_path=self.expected)
        assert_export_was_published(self, self.output, self.remote)

    def test_stale_metadata_refuses_write_without_leaking_source_values(self):
        self.remote.seed(self.expected)
        self.remote.connection.execute("UPDATE catalog_meta SET value='concurrent' WHERE key='generated_at'")
        before = digest(self.remote.connection)
        with self.assertRaises(TursoError) as error:
            publish_turso.publish(self.output, self.remote, expected_snapshot_path=self.expected)
        self.assertNotIn('100', str(error.exception))
        self.assertEqual(before, digest(self.remote.connection))

    def test_stale_price_inside_model_json_refuses_write_without_leaking_price(self):
        self.remote.seed(self.expected)
        current = json.loads(self.remote.connection.execute(
            'SELECT model_json FROM published_models WHERE product_id=1').fetchone()[0])
        current['prices'][0]['amount'] = '999999-secret-synthetic'
        self.remote.connection.execute('UPDATE published_models SET model_json=? WHERE product_id=1',
                                       (json.dumps(current),))
        before = digest(self.remote.connection)
        with self.assertRaises(TursoError) as error:
            publish_turso.publish(self.output, self.remote, expected_snapshot_path=self.expected)
        self.assertNotIn('999999-secret-synthetic', str(error.exception))
        self.assertEqual(before, digest(self.remote.connection))
        self.assertFalse(self.remote.connection.in_transaction)
        self.assertIsNone(self.remote.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='_ups_publish_snapshot_guard'").fetchone())

    def test_failed_rollback_is_reported_as_unknown_instead_of_claiming_success(self):
        self.remote.seed(self.expected)
        self.remote.connection.execute("UPDATE catalog_meta SET value='concurrent' WHERE key='generated_at'")
        self.remote.fail_rollback = True
        try:
            with self.assertRaisesRegex(TursoError, 'rollback status is unknown'):
                publish_turso.publish(self.output, self.remote, expected_snapshot_path=self.expected)
            self.assertTrue(self.remote.connection.in_transaction)
        finally:
            if self.remote.connection.in_transaction:
                self.remote.connection.execute('ROLLBACK')

    def test_changed_technical_column_or_row_count_refuses_write(self):
        for mutation in (
                "UPDATE published_models SET product_id=3 WHERE product_id=1",
                "UPDATE published_models SET manufacturer='Other' WHERE product_id=1",
                "UPDATE published_models SET model='Other' WHERE product_id=1",
                "UPDATE published_models SET sku='Other' WHERE product_id=1",
                "UPDATE published_models SET rating_w=1 WHERE product_id=1",
                "UPDATE published_models SET rating_va=1 WHERE product_id=1",
                "UPDATE published_models SET outlet_count=1 WHERE product_id=1",
                "DELETE FROM published_models WHERE product_id=1"):
            with self.subTest(mutation=mutation):
                remote = SqliteRemote()
                self.addCleanup(remote.close)
                remote.seed(self.expected)
                remote.connection.execute(mutation)
                before = digest(remote.connection)
                with self.assertRaises(TursoError):
                    publish_turso.publish(self.output, remote, expected_snapshot_path=self.expected)
                self.assertEqual(before, digest(remote.connection))

    def test_change_after_preflight_is_detected_inside_transaction(self):
        self.remote.seed(self.expected)
        state = {}
        def mutate(connection):
            connection.execute("UPDATE catalog_meta SET value='concurrent' WHERE key='generated_at'")
            state['digest'] = digest(connection)
        self.remote.before_begin = mutate
        with self.assertRaises(TursoError):
            publish_turso.publish(self.output, self.remote, expected_snapshot_path=self.expected)
        self.assertIn('digest', state)
        self.assertEqual(state['digest'], digest(self.remote.connection))

    def test_unrelated_table_added_after_preflight_blocks_publication(self):
        self.remote.seed(self.expected)
        before = digest(self.remote.connection)
        self.remote.before_begin = lambda connection: connection.execute(
            'CREATE TABLE unrelated_state(id INTEGER PRIMARY KEY)')
        with self.assertRaises(TursoError):
            publish_turso.publish(self.output, self.remote, expected_snapshot_path=self.expected)
        self.assertEqual(before, digest(self.remote.connection))
        self.assertTrue(self.remote.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='unrelated_state'").fetchone())

    def test_remote_trigger_cannot_modify_published_rows(self):
        self.remote.seed(self.expected)
        self.remote.connection.executescript('''
            CREATE TRIGGER corrupt_insert AFTER INSERT ON published_models
            BEGIN UPDATE published_models SET model_json='{}' WHERE product_id=NEW.product_id; END;
        ''')
        before = digest(self.remote.connection)
        with self.assertRaises(TursoError):
            publish_turso.publish(self.output, self.remote, expected_snapshot_path=self.expected)
        self.assertEqual(before, digest(self.remote.connection))
        self.assertFalse(self.remote.connection.in_transaction)

    def test_sqlite_like_prefix_is_not_confused_with_an_internal_table(self):
        self.remote.seed(self.expected)
        before = digest(self.remote.connection)
        self.remote.before_begin = lambda connection: connection.execute(
            'CREATE TABLE sqliteX_unrelated(id INTEGER PRIMARY KEY)')
        with self.assertRaises(TursoError):
            publish_turso.publish(self.output, self.remote, expected_snapshot_path=self.expected)
        self.assertEqual(before, digest(self.remote.connection))

    def test_snapshot_comparison_is_binary_even_if_remote_has_nocase_collation(self):
        for table in ('catalog_meta', 'published_models'):
            with self.subTest(table=table):
                remote = SqliteRemote()
                self.addCleanup(remote.close)
                remote.seed(self.expected)
                if table == 'catalog_meta':
                    remote.connection.executescript('''
                        ALTER TABLE catalog_meta RENAME TO old_meta;
                        CREATE TABLE catalog_meta(key TEXT PRIMARY KEY, value TEXT COLLATE NOCASE NOT NULL);
                        INSERT INTO catalog_meta SELECT * FROM old_meta;
                        DROP TABLE old_meta;
                        UPDATE catalog_meta SET value='OLD' WHERE key='generated_at';
                    ''')
                else:
                    remote.connection.executescript('''
                        ALTER TABLE published_models RENAME TO old_models;
                        CREATE TABLE published_models(product_id INTEGER PRIMARY KEY,
                            manufacturer TEXT,model TEXT,sku TEXT,rating_w REAL,rating_va REAL,
                            outlet_count INTEGER,model_json TEXT COLLATE NOCASE NOT NULL);
                        INSERT INTO published_models SELECT * FROM old_models;
                        DROP TABLE old_models;
                        UPDATE published_models SET model_json=replace(model_json,'old','OLD')
                            WHERE product_id=1;
                    ''')
                before = digest(remote.connection)
                with self.assertRaises(TursoError):
                    publish_turso.publish(self.output, remote, expected_snapshot_path=self.expected)
                self.assertEqual(before, digest(remote.connection))

    def test_snapshot_comparison_rejects_a_change_in_sqlite_value_type(self):
        self.remote.seed(self.expected)
        self.remote.connection.executescript('''
            ALTER TABLE published_models RENAME TO old_models;
            CREATE TABLE published_models(product_id INTEGER PRIMARY KEY,
                manufacturer TEXT,model TEXT,sku TEXT,rating_w TEXT,rating_va REAL,
                outlet_count INTEGER,model_json TEXT NOT NULL);
            INSERT INTO published_models SELECT * FROM old_models;
            DROP TABLE old_models;
        ''')
        before = digest(self.remote.connection)
        with self.assertRaises(TursoError):
            publish_turso.publish(self.output, self.remote, expected_snapshot_path=self.expected)
        self.assertEqual(before, digest(self.remote.connection))

    def test_empty_database_populated_after_preflight_is_not_overwritten(self):
        def concurrent_initial_publish(connection):
            with closing(sqlite3.connect(self.expected)) as source:
                source.backup(connection)
        self.remote.before_begin = concurrent_initial_publish
        with self.assertRaises(TursoError):
            publish_turso.publish(self.output, self.remote)
        with closing(sqlite3.connect(self.expected)) as expected:
            self.assertEqual(digest(expected), digest(self.remote.connection))

    def test_expected_snapshot_must_have_valid_schema_format_and_model_count(self):
        self.remote.seed(self.expected)
        missing = Path(self.temp.name) / 'missing.sqlite3'
        bad_schema = Path(self.temp.name) / 'bad-schema.sqlite3'
        with closing(sqlite3.connect(bad_schema)) as connection:
            connection.execute('CREATE TABLE anything(value TEXT)')
        with self.assertRaisesRegex(ValueError, 'snapshot'):
            publish_turso.publish(self.output, self.remote, expected_snapshot_path=missing)
        with self.assertRaisesRegex(ValueError, 'schema'):
            publish_turso.publish(self.output, self.remote, expected_snapshot_path=bad_schema)
        for key, value in (('format', 'unsupported'), ('model_count', '3')):
            with self.subTest(key=key):
                bad = Path(self.temp.name) / f'bad-{key}.sqlite3'
                write_catalog(bad)
                with closing(sqlite3.connect(bad)) as connection:
                    connection.execute('UPDATE catalog_meta SET value=? WHERE key=?', (value, key))
                    connection.commit()
                with self.assertRaises(ValueError):
                    publish_turso.publish(self.output, self.remote, expected_snapshot_path=bad)
        self.assertEqual(0, self.remote.pipeline_calls)

    def test_expected_snapshot_with_nullable_required_metadata_is_rejected(self):
        self.remote.seed(self.expected)
        malformed = Path(self.temp.name) / 'nullable-metadata.sqlite3'
        with closing(sqlite3.connect(self.expected)) as source, closing(sqlite3.connect(malformed)) as copy:
            copy.executescript(SCHEMA.replace('value TEXT NOT NULL', 'value TEXT'))
            copy.executemany('INSERT INTO catalog_meta VALUES(?,?)',
                             source.execute('SELECT key,value FROM catalog_meta'))
            copy.executemany('INSERT INTO published_models VALUES(?,?,?,?,?,?,?,?)',
                             source.execute('SELECT * FROM published_models'))
            copy.commit()
        with self.assertRaisesRegex(ValueError, 'schema'):
            publish_turso.publish(self.output, self.remote, expected_snapshot_path=malformed)
        self.assertEqual(0, self.remote.pipeline_calls)

    def test_request_size_limit_counts_expected_snapshot_and_output(self):
        normal = SqliteRemote()
        self.addCleanup(normal.close)
        normal.seed(self.expected)
        publish_turso.publish(self.output, normal, expected_snapshot_path=self.expected)
        self.assertGreater(normal.last_body_size, 0)
        large_expected = Path(self.temp.name) / 'large.sqlite3'
        write_catalog(large_expected, version='ups-curated-v1', padding='x' * 2500)
        bind_export_to_source(self.output, large_expected)
        self.remote.seed(large_expected)
        with patch.object(publish_turso, 'MAX_BATCH_BYTES', normal.last_body_size + 100):
            with self.assertRaisesRegex(ValueError, 'size|exceeds'):
                publish_turso.publish(self.output, self.remote, expected_snapshot_path=large_expected)
        self.assertEqual(0, self.remote.pipeline_calls)


if __name__ == '__main__':
    unittest.main()
