import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.db.backup import backup_sqlite_db, cleanup_old_backups


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.source = self.root / 'source.db'
        self.backups = self.root / 'backups'
        self.backups.mkdir()
        self.conn = sqlite3.connect(self.source)
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA wal_autocheckpoint=0')
        self.conn.execute('CREATE TABLE example(value TEXT)')
        self.conn.execute("INSERT INTO example VALUES('未checkpoint的数据')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.directory.cleanup()

    def backup(self, **kwargs):
        return backup_sqlite_db(db_path=self.source, backup_dir=self.backups, **kwargs)

    def test_wal_snapshot_becomes_standalone_database(self):
        result = self.backup()
        with sqlite3.connect(result) as conn:
            self.assertEqual(conn.execute('SELECT value FROM example').fetchall(),
                             [('未checkpoint的数据',)])
            self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
            self.assertEqual(conn.execute('PRAGMA quick_check').fetchone()[0], 'ok')
        self.assertEqual(list(self.backups.iterdir()), [result])

    def test_low_space_preserves_previous_backup(self):
        previous = self.backups / 'libot_20260101_050000.db'
        previous.write_bytes(b'previous')
        with patch('src.db.backup.shutil.disk_usage', return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(RuntimeError, '空间不足'):
                self.backup()
        self.assertEqual(previous.read_bytes(), b'previous')
        self.assertEqual(list(self.backups.iterdir()), [previous])

    def test_publish_failure_removes_only_temporary_files(self):
        previous = self.backups / 'libot_20260101_050000.db'
        previous.write_bytes(b'previous')
        with patch.object(Path, 'replace', side_effect=OSError('publish failed')):
            with self.assertRaisesRegex(RuntimeError, 'publish failed'):
                self.backup()
        self.assertEqual(list(self.backups.iterdir()), [previous])

    def test_deadline_preserves_previous_backup(self):
        previous = self.backups / 'libot_20260101_050000.db'
        previous.write_bytes(b'previous')
        with patch('src.db.backup.time.monotonic', side_effect=[0, 100]):
            with self.assertRaisesRegex(RuntimeError, '执行时限'):
                self.backup(max_seconds=1)
        self.assertEqual(list(self.backups.iterdir()), [previous])

    def test_retention_removes_old_sidecars_and_keeps_manual_backup(self):
        names = ['libot_20260101_050000.db', 'libot_20260102_050000.db',
                 'libot_20260103_050000.db', 'libot_manual.db']
        for name in names:
            for suffix in ('', '-wal', '-shm'):
                (self.backups / (name+suffix)).write_bytes(b'data')
        cleanup_old_backups(self.backups, keep_count=2)
        for name in names:
            for suffix in ('', '-wal', '-shm'):
                self.assertEqual((self.backups / (name+suffix)).exists(), name != names[0])

    def test_invalid_limits_are_rejected(self):
        for kwargs in ({'keep_count': 0}, {'max_seconds': 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.backup(**kwargs)

    def test_default_retention_keeps_only_latest_backup(self):
        old = self.backups / 'libot_20260101_050000.db'
        old.write_bytes(b'previous')
        result = self.backup()
        self.assertFalse(old.exists())
        self.assertEqual(list(self.backups.iterdir()), [result])

    def test_cleanup_default_keeps_only_latest_backup(self):
        old = self.backups / 'libot_20260101_050000.db'
        latest = self.backups / 'libot_20260102_050000.db'
        old.write_bytes(b'previous')
        latest.write_bytes(b'latest')
        cleanup_old_backups(self.backups)
        self.assertEqual(list(self.backups.iterdir()), [latest])
