"""Activation recovery with real temporary SQLite and mocked service commands."""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parent.parent / 'scripts' / 'deploy' / 'runtime.py'
SPEC = importlib.util.spec_from_file_location('runtime_migration_fixture', SOURCE)
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)
OLD, NEW = 'a' * 40, 'b' * 40


class ActivationMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='watch-activation-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / 'shared' / 'data'
        self.data.mkdir(parents=True)
        self.database = self.data / 'watch.sqlite3'
        self.marker = self.data / '.activation-in-progress'
        self.service, self.site, self.proxy = (self.root / name for name in ('backend.service', 'site.conf', 'proxy.conf'))
        self.original_site = 'server {\n    listen 8088;\n    server_name original.example.test;\n    location / { return 200; }\n}\n'
        self.service.write_text('old unit', encoding='utf-8')
        self.site.write_text(self.original_site, encoding='utf-8')
        self.proxy.write_text('proxy_set_header Host original.example.test;\n', encoding='utf-8')
        for sha, schema in ((OLD, 1), (NEW, 2)):
            release = self.root / 'releases' / sha
            release.mkdir(parents=True)
            (release / 'release.json').write_text(json.dumps({'kind': 'monitoring-server', 'dataSchema': schema}))
        with contextlib.closing(sqlite3.connect(str(self.database))) as db:
            db.executescript("CREATE TABLE preserved(credential TEXT, history TEXT); INSERT INTO preserved VALUES('unchanged-secret','actual-old-history'); PRAGMA user_version=1;")
        self.database.chmod(0o600)
        self.calls = []
        self.active = True
        self.fail_init = self.fail_stop = False
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in (('SERVICE', self.service), ('SITE', self.site), ('PROXY', self.proxy)):
            self.stack.enter_context(patch.object(runtime, name, value))
        self.stack.enter_context(patch.object(runtime, 'data_directory', return_value=self.data))
        self.stack.enter_context(patch.object(runtime, 'run', side_effect=self.run_command))
        self.activation = self.make_activation()

    def make_activation(self, current=OLD):
        # Bypass the production root/platform constructor only. Every method
        # under test operates real files and SQLite in this temporary tree.
        value = object.__new__(runtime.Activation)
        value.root, value.release, value.port = self.root, self.root / 'releases' / NEW, 8091
        value.original_release, value.target_schema = current, 2
        value.owner_uid = os.geteuid() if hasattr(os, 'geteuid') else 0
        value.config, value.domains = {}, []
        value.snapshots = {self.service: self.service.read_text(), self.site: self.site.read_text(), self.proxy: self.proxy.read_text()}
        value.was_active = value.was_enabled = True
        value.changed = value.start_attempted = value.units_touched = value.stop_attempted = False
        value.marker_active = value.committed = value.recovery_blocked = False
        value.backup = value.directory = None
        return value

    def run_command(self, arguments, capture=True):
        self.calls.append(list(arguments))
        if '--init-admin' in arguments:
            self.assertFalse(self.active, 'Old backend must stop before migration')
            self.assertTrue(self.marker.is_file(), 'Maintenance gate must precede migration')
            with contextlib.closing(sqlite3.connect(str(self.database))) as db:
                schema = db.execute('PRAGMA user_version').fetchone()[0]
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if schema in (0, 1) and tables:
                    db.execute('ALTER TABLE preserved ADD COLUMN v2 INTEGER DEFAULT 2')
                    db.execute('PRAGMA user_version=2')
                    db.commit()
                elif schema == 0:
                    db.executescript('CREATE TABLE preserved(credential TEXT,history TEXT,v2 INTEGER); PRAGMA user_version=2;')
            if self.fail_init:
                raise ValueError('fixture CLI fails after schema migration')
        elif arguments[:2] == ['systemctl', 'stop']:
            if self.fail_stop:
                raise ValueError('fixture service could not stop')
            self.active = False
        elif arguments[:2] == ['systemctl', 'show']:
            return 'active\n' if self.active else 'inactive\n'
        elif arguments[:2] == ['systemctl', 'start']:
            self.assertFalse(self.marker.exists(), 'Writes reopen only after database/config restoration')
            self.active = True
        elif arguments[:2] == ['systemctl', 'restart']:
            self.active = True
        return ''

    def state(self):
        with contextlib.closing(sqlite3.connect(str(self.database))) as db:
            return db.execute('PRAGMA user_version').fetchone()[0], db.execute('SELECT credential,history FROM preserved').fetchall()

    def test_stop_private_snapshot_gate_migrate_and_commit(self):
        self.activation.prepare()
        self.assertEqual(self.state(), (2, [('unchanged-secret', 'actual-old-history')]))
        self.assertIsNotNone(self.activation.backup)
        self.assertEqual(self.activation._database_schema(self.activation.backup), 1)
        self.assertEqual(self.activation._marker_read()['backup'], self.activation.backup.name)
        if os.name == 'posix':
            self.assertEqual(self.marker.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.activation.backup.stat().st_mode & 0o777, 0o600)
        self.activation.commit()
        self.assertFalse(self.marker.exists())
        before = len(self.calls)
        self.activation.restore()
        self.assertEqual(len(self.calls), before, 'A published activation cannot restore the old database')

    def test_cli_failure_before_changed_flag_restores_database_then_old_service(self):
        self.fail_init = True
        with self.assertRaises(ValueError):
            self.activation.prepare()
        self.assertFalse(self.activation.changed)
        self.activation.restore()
        self.assertEqual(self.state(), (1, [('unchanged-secret', 'actual-old-history')]))
        self.assertTrue(self.active)
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.site.read_text(), self.original_site)

    def test_health_failure_after_new_service_started_restores_schema_one(self):
        self.activation.prepare()
        self.activation.start_attempted = self.activation.units_touched = True
        self.active = True
        # WAL/SHM must not survive replacement with the saved whole database.
        for suffix in ('-wal', '-shm'):
            self.database.with_name(self.database.name + suffix).write_bytes(b'fixture stale sidecar')
        self.activation.restore()
        self.assertEqual(self.state(), (1, [('unchanged-secret', 'actual-old-history')]))
        self.assertTrue(self.active)
        self.assertFalse(self.marker.exists())
        self.assertFalse(self.database.with_name(self.database.name + '-wal').exists())

    def test_snapshot_failure_never_migrates_and_stopped_service_is_restored(self):
        with patch.object(self.activation, '_snapshot_database', side_effect=OSError('fixture disk full')):
            with self.assertRaises(OSError):
                self.activation.prepare()
        self.assertFalse(any('--init-admin' in call for call in self.calls))
        self.activation.restore()
        self.assertTrue(self.active)
        self.assertEqual(self.state()[0], 1)

    def test_failed_stop_never_touches_database_and_restore_does_not_start_again(self):
        self.fail_stop = True
        with self.assertRaises(ValueError):
            self.activation.prepare()
        with self.assertRaises(ValueError):
            self.activation.restore()
        self.assertFalse(any('--init-admin' in call or call[:2] == ['systemctl', 'start'] for call in self.calls))
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.state()[0], 1)

    def test_database_restore_failure_keeps_gate_backup_and_old_service_stopped(self):
        self.activation.prepare()
        with patch.object(self.activation, '_restore_database', side_effect=OSError('fixture failed atomic restore')):
            with self.assertRaisesRegex(ValueError, '数据库恢复失败'):
                self.activation.restore()
        self.assertTrue(self.marker.is_file())
        self.assertTrue(self.activation.backup.is_file())
        self.assertFalse(self.active)
        self.assertFalse(any(call[:2] == ['systemctl', 'start'] for call in self.calls))

    def test_commit_marker_unlink_failure_remains_safe_to_restore(self):
        self.activation.prepare()
        original_unlink = Path.unlink
        def fail_marker(path, *args, **kwargs):
            if path == self.marker:
                raise OSError('fixture cannot publish')
            return original_unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', fail_marker):
            with self.assertRaises(OSError):
                self.activation.commit()
        self.assertFalse(self.activation.committed)
        self.activation.restore()
        self.assertEqual(self.state()[0], 1)

    def test_crash_with_old_current_recovers_owned_backup_before_retry_migration(self):
        self.activation.prepare()  # Simulate process loss: marker+schema2 remain, old current still selected.
        retry = self.make_activation()
        retry.prepare()
        self.assertEqual(retry.journal['databaseSchema'], 1)
        self.assertEqual(retry._database_schema(retry.backup), 1)
        retry.restore()
        self.assertEqual(self.state(), (1, [('unchanged-secret', 'actual-old-history')]))

    def test_crash_after_current_switched_schema2_resumes_without_schema1_rewind(self):
        self.activation.prepare()
        retry = self.make_activation(current=NEW)
        retry.prepare()
        self.assertIsNone(retry.backup)
        self.assertEqual(retry.journal['databaseSchema'], 2)
        retry.commit()
        self.assertEqual(self.state()[0], 2)

    def test_unowned_or_inconsistent_residual_marker_never_removes_or_starts_incompatible_old_code(self):
        self.activation.prepare()
        original = self.marker.read_bytes()
        data = json.loads(original)
        data['backup'] = '../foreign.sqlite3'
        self.marker.write_text(json.dumps(data))
        retry = self.make_activation()
        with self.assertRaises(ValueError):
            retry.prepare()
        with self.assertRaises(ValueError):
            retry.restore()
        self.assertEqual(json.loads(self.marker.read_text())['backup'], '../foreign.sqlite3')
        self.assertFalse(self.active)
        self.assertEqual(self.state()[0], 2)

    def test_first_schema2_install_commits_without_old_database_or_backup(self):
        self.database.unlink()
        self.activation.original_release = None
        self.activation.snapshots[self.service] = None
        self.activation.was_active = False
        self.active = False
        self.activation.prepare()
        self.assertIsNone(self.activation.backup)
        self.activation.commit()
        self.assertFalse(self.marker.exists())
        self.assertEqual(self.state()[0], 2)

    def test_real_v03_raw_zero_database_is_backed_up_and_failed_cli_restores_credentials_history(self):
        with contextlib.closing(sqlite3.connect(str(self.database))) as db:
            db.execute('PRAGMA user_version=0')
        self.fail_init = True
        with self.assertRaises(ValueError):
            self.activation.prepare()
        self.assertIn('activation-schema0-', self.activation.backup.name)
        self.assertEqual(self.activation._database_schema(self.activation.backup), 0)
        self.activation.restore()
        self.assertEqual(self.state(), (0, [('unchanged-secret', 'actual-old-history')]))
        self.assertTrue(self.active)

    def test_crashed_raw_zero_legacy_migration_recovers_before_repeating_upgrade(self):
        with contextlib.closing(sqlite3.connect(str(self.database))) as db:
            db.execute('PRAGMA user_version=0')
        self.activation.prepare()
        retry = self.make_activation()
        retry.prepare()
        self.assertEqual(retry.journal['databaseSchema'], 0)
        self.assertEqual(retry._database_schema(retry.backup), 0)
        retry.restore()
        self.assertEqual(self.state(), (0, [('unchanged-secret', 'actual-old-history')]))

    @unittest.skipUnless(os.name == 'posix', 'Real POSIX permission enforcement')
    def test_residual_marker_with_wrong_permissions_cannot_resume_or_reopen_old_service(self):
        self.activation.prepare()
        self.marker.chmod(0o644)
        retry = self.make_activation()
        with self.assertRaises(ValueError):
            retry.prepare()
        with self.assertRaises(ValueError):
            retry.restore()
        self.assertTrue(self.marker.exists())
        self.assertFalse(self.active)

    def test_existing_service_with_missing_database_is_not_restarted_with_a_new_empty_database(self):
        self.database.unlink()
        with self.assertRaises(ValueError):
            self.activation.prepare()
        with self.assertRaises(ValueError):
            self.activation.restore()
        self.assertFalse(any(call[:2] == ['systemctl', 'start'] for call in self.calls))


if __name__ == '__main__':
    unittest.main()
