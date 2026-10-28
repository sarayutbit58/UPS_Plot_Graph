"""Check that a staged catalog can only replace its actual base snapshot."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from database import append_verified_catalog, publish_turso, stage_verified_catalog
from database.tests import test_append_verified_catalog as append_fixture
from database.tests import test_stage_verified_catalog as stage_fixture
from database.tests.test_publisher_concurrency import SqliteRemote, assert_export_was_published, digest


class StagePublicationLineageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.a = root / 'source-a.sqlite3'
        self.b = root / 'source-b.sqlite3'
        self.stage = root / 'staged.sqlite3'
        self.raw = root / 'raw.sqlite3'
        self.remote = SqliteRemote()
        self.addCleanup(self.remote.close)
        stage_fixture.write_snapshot(self.a)

    def copy_a_to_b(self):
        with closing(sqlite3.connect(self.a)) as source, closing(sqlite3.connect(self.b)) as dest:
            source.backup(dest)

    def fingerprint(self, path):
        return publish_turso.snapshot_fingerprint(*publish_turso._read_catalog(path, expected=True))

    def test_stage_binds_all_original_data_without_storing_plaintext_price(self):
        stage_verified_catalog.stage_catalog(self.a, self.stage, [])
        meta, _ = stage_fixture.read_snapshot(self.stage)
        self.assertRegex(meta['base_snapshot_sha256'], r'^[0-9a-f]{64}$')
        self.assertEqual(meta['base_snapshot_sha256'], self.fingerprint(self.a))
        self.assertNotIn('123.4500', meta['base_snapshot_sha256'])

    def test_wrong_base_is_rejected_even_when_expected_matches_remote(self):
        stage_verified_catalog.stage_catalog(self.a, self.stage, [])
        self.copy_a_to_b()
        with closing(sqlite3.connect(self.b)) as db, db:
            db.execute("UPDATE published_models SET model_json=replace(model_json, '123.4500', '654.321')")
        self.remote.seed(self.b)
        before = digest(self.remote.connection)
        with patch.object(self.remote, 'query', wraps=self.remote.query) as query:
            with self.assertRaisesRegex(ValueError, 'base|fingerprint') as raised:
                publish_turso.publish(self.stage, self.remote, expected_snapshot_path=self.b)
            query.assert_not_called()
        self.assertEqual(0, self.remote.pipeline_calls)
        self.assertEqual(before, digest(self.remote.connection))
        self.assertNotIn('654.321', str(raised.exception))
        self.assertNotIn('123.4500', str(raised.exception))

    def test_matching_base_passes_the_transaction_guard(self):
        stage_verified_catalog.stage_catalog(self.a, self.stage, [])
        self.remote.seed(self.a)
        publish_turso.publish(self.stage, self.remote, expected_snapshot_path=self.a)
        assert_export_was_published(self, self.stage, self.remote)
        self.assertEqual(1, self.remote.pipeline_calls)

    def test_fingerprint_captures_metadata_case_json_and_sqlite_types(self):
        metadata, models = publish_turso._read_catalog(self.a, expected=True)
        fingerprint = publish_turso.snapshot_fingerprint(metadata, models)
        self.assertEqual(fingerprint, publish_turso.snapshot_fingerprint(list(reversed(metadata)),
                                                                         list(reversed(models))))
        changed_meta = [(key, value.upper() if key == 'generated_at' else value) for key, value in metadata]
        # Change a real alphabetic byte; generated_at fixture itself has digits only.
        changed_meta = [(key, value.upper() if key == 'custom_marker' else value) for key, value in changed_meta]
        self.assertNotEqual(fingerprint, publish_turso.snapshot_fingerprint(changed_meta, models))
        row = list(models[0])
        row[-1] += ' '
        self.assertNotEqual(fingerprint, publish_turso.snapshot_fingerprint(metadata, [tuple(row)] + models[1:]))
        row = list(models[0])
        row[4] = int(row[4])
        self.assertNotEqual(fingerprint, publish_turso.snapshot_fingerprint(metadata, [tuple(row)] + models[1:]))
        row = list(models[0])
        row[1] = row[1].lower()
        self.assertNotEqual(fingerprint, publish_turso.snapshot_fingerprint(metadata, [tuple(row)] + models[1:]))

    def test_staging_multiple_manifests_preserves_original_base_and_publishes(self):
        stage_verified_catalog.stage_catalog(self.a, self.stage, [stage_fixture.maker_record()])
        second = self.stage.with_name('next-stage.sqlite3')
        stage_verified_catalog.stage_catalog(
            self.stage, second, [stage_fixture.maker_record('CyberPower', 'CL-1')])
        first_meta, _ = stage_fixture.read_snapshot(self.stage)
        second_meta, rows = stage_fixture.read_snapshot(second)
        self.assertEqual(first_meta['base_snapshot_sha256'], self.fingerprint(self.a))
        self.assertEqual(second_meta['base_snapshot_sha256'], first_meta['base_snapshot_sha256'])
        self.assertTrue(json.loads(rows[0][-1])['identity_verified'])
        self.assertTrue(json.loads(rows[1][-1])['identity_verified'])
        self.remote.seed(self.a)
        publish_turso.publish(second, self.remote, expected_snapshot_path=self.a)
        assert_export_was_published(self, second, self.remote)

    def test_invalid_staged_marker_is_rejected_before_creating_output(self):
        stage_verified_catalog.stage_catalog(self.a, self.stage, [])
        for index, value in enumerate(('', 'x' * 64, 'A' * 64, 'not-a-digest')):
            with self.subTest(value=value):
                second = self.stage.with_name(f'bad-stage-{index}.sqlite3')
                with closing(sqlite3.connect(self.stage)) as db, db:
                    db.execute("UPDATE catalog_meta SET value=? WHERE key='base_snapshot_sha256'", (value,))
                with self.assertRaisesRegex(ValueError, 'base|fingerprint'):
                    stage_verified_catalog.stage_catalog(self.stage, second, [])
                self.assertFalse(second.exists())

    def test_forged_digest_cannot_publish_over_original_base(self):
        stage_verified_catalog.stage_catalog(self.a, self.stage, [])
        with closing(sqlite3.connect(self.stage)) as db, db:
            db.execute("UPDATE catalog_meta SET value=? WHERE key='base_snapshot_sha256'", ('0' * 64,))
        second = self.stage.with_name('next-stage.sqlite3')
        stage_verified_catalog.stage_catalog(self.stage, second, [])
        self.remote.seed(self.a)
        before = digest(self.remote.connection)
        with patch.object(self.remote, 'query', wraps=self.remote.query) as query:
            with self.assertRaisesRegex(ValueError, 'base|fingerprint'):
                publish_turso.publish(second, self.remote, expected_snapshot_path=self.a)
            query.assert_not_called()
        self.assertEqual(0, self.remote.pipeline_calls)
        self.assertEqual(before, digest(self.remote.connection))

    def test_published_backup_can_be_appended_and_published_again(self):
        stage_verified_catalog.stage_catalog(self.a, self.stage, [])
        self.remote.seed(self.a)
        publish_turso.publish(self.stage, self.remote, expected_snapshot_path=self.a)
        with closing(sqlite3.connect(self.b)) as backup:
            self.remote.connection.backup(backup)
        published_meta, _ = stage_fixture.read_snapshot(self.b)
        self.assertNotIn('base_snapshot_sha256', published_meta)
        append_fixture.write_raw(self.raw)
        append_fixture.add_raw(self.raw, 10, 'NEW-10')
        next_output = self.stage.with_name('next-append.sqlite3')
        append_verified_catalog.append_catalog(self.b, self.raw, next_output,
                                                [append_fixture.maker_record('NEW-10')])
        next_meta, _ = stage_fixture.read_snapshot(next_output)
        self.assertEqual(next_meta['base_snapshot_sha256'], self.fingerprint(self.b))
        publish_turso.publish(next_output, self.remote, expected_snapshot_path=self.b)
        remote_meta = dict(self.remote.query('SELECT key,value FROM catalog_meta'))
        self.assertNotIn('base_snapshot_sha256', remote_meta)
        self.assertEqual(4, self.remote.query('SELECT count(*) FROM published_models')[0][0])

    def test_new_stage_from_published_backup_fingerprints_current_snapshot(self):
        stage_verified_catalog.stage_catalog(self.a, self.stage, [])
        self.remote.seed(self.a)
        publish_turso.publish(self.stage, self.remote, expected_snapshot_path=self.a)
        with closing(sqlite3.connect(self.b)) as backup:
            self.remote.connection.backup(backup)
        second = self.stage.with_name('next-stage.sqlite3')
        stage_verified_catalog.stage_catalog(self.b, second, [])
        first_meta, _ = stage_fixture.read_snapshot(self.stage)
        second_meta, _ = stage_fixture.read_snapshot(second)
        self.assertEqual(second_meta['base_snapshot_sha256'], self.fingerprint(self.b))
        self.assertNotEqual(second_meta['base_snapshot_sha256'], first_meta['base_snapshot_sha256'])
        publish_turso.publish(second, self.remote, expected_snapshot_path=self.b)
        assert_export_was_published(self, second, self.remote)

    def test_append_preserves_original_staged_base(self):
        stage_verified_catalog.stage_catalog(self.a, self.stage, [])
        append_fixture.write_raw(self.raw)
        append_fixture.add_raw(self.raw, 10, 'NEW-10')
        output = self.stage.with_name('appended.sqlite3')
        append_verified_catalog.append_catalog(self.stage, self.raw, output,
                                                [append_fixture.maker_record('NEW-10')])
        staged_meta, _ = stage_fixture.read_snapshot(self.stage)
        appended_meta, _ = stage_fixture.read_snapshot(output)
        self.assertEqual(appended_meta['base_snapshot_sha256'], staged_meta['base_snapshot_sha256'])
        self.assertEqual(appended_meta['base_snapshot_sha256'], self.fingerprint(self.a))
        self.remote.seed(self.a)
        publish_turso.publish(output, self.remote, expected_snapshot_path=self.a)
        assert_export_was_published(self, output, self.remote)

    def test_append_to_unstaged_snapshot_binds_that_current_snapshot(self):
        append_fixture.write_snapshot(self.b)
        append_fixture.write_raw(self.raw)
        append_fixture.add_raw(self.raw, 10, 'NEW-10')
        append_verified_catalog.append_catalog(self.b, self.raw, self.stage,
                                                [append_fixture.maker_record('NEW-10')])
        meta, _ = stage_fixture.read_snapshot(self.stage)
        self.assertEqual(meta['base_snapshot_sha256'], self.fingerprint(self.b))
        self.remote.seed(self.b)
        publish_turso.publish(self.stage, self.remote, expected_snapshot_path=self.b)
        assert_export_was_published(self, self.stage, self.remote)

    def test_append_rejects_raw_json_change_between_snapshot_reads(self):
        stage_verified_catalog.stage_catalog(self.a, self.stage, [])
        append_fixture.write_raw(self.raw)
        output = self.stage.with_name('appended.sqlite3')
        read = append_verified_catalog._read_snapshot

        def mutate_after_validation(path, **kwargs):
            result = read(path, **kwargs)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("UPDATE published_models SET model_json=model_json || ' ' WHERE product_id=1")
            return result

        with patch.object(append_verified_catalog, '_read_snapshot', side_effect=mutate_after_validation):
            with self.assertRaisesRegex(ValueError, 'changed'):
                append_verified_catalog.append_catalog(self.stage, self.raw, output, [])
        self.assertFalse(output.exists())

    def test_unbound_output_cannot_overwrite_existing_catalog(self):
        self.copy_a_to_b()
        with closing(sqlite3.connect(self.b)) as db, db:
            db.execute("UPDATE catalog_meta SET value='ups-curated-v2' WHERE key='format'")
        self.remote.seed(self.b)
        before = digest(self.remote.connection)
        with self.assertRaisesRegex(ValueError, 'base|fingerprint'):
            publish_turso.publish(self.b, self.remote, expected_snapshot_path=self.b)
        self.assertEqual(0, self.remote.pipeline_calls)
        self.assertEqual(before, digest(self.remote.connection))

    def test_malformed_base_marker_is_rejected_before_remote_access(self):
        stage_verified_catalog.stage_catalog(self.a, self.stage, [])
        with closing(sqlite3.connect(self.stage)) as db, db:
            db.execute("UPDATE catalog_meta SET value='invalid-secret' WHERE key='base_snapshot_sha256'")
        with patch.object(self.remote, 'query', wraps=self.remote.query) as query:
            with self.assertRaisesRegex(ValueError, 'base|fingerprint') as raised:
                publish_turso.publish(self.stage, self.remote, expected_snapshot_path=self.a)
            query.assert_not_called()
        self.assertEqual(0, self.remote.pipeline_calls)
        self.assertNotIn('invalid-secret', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
