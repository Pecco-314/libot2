import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.db import danmaku_sources as store
from scripts import migrate_danmaku_cold_storage as migration


class ColdMigrationTest(unittest.TestCase):
    def test_drain_refuses_unverified_rows_and_non_archive_tables(self):
        with tempfile.TemporaryDirectory() as d, patch.object(store,'PRODUCTION_DB',Path(d)/'live.db'), sqlite3.connect(':memory:') as source, sqlite3.connect(':memory:') as cold:
            for conn in (source,cold):
                conn.executescript(store.SCHEMA)
                conn.execute("INSERT INTO danmaku_source_checkpoint VALUES('test','original','now')")
                conn.commit()
            cold.execute("UPDATE danmaku_source_checkpoint SET value='different'")
            with self.assertRaisesRegex(RuntimeError,'Cold batch mismatch'):
                migration.drain_table(source,cold,'danmaku_source_checkpoint')
            self.assertEqual(source.execute('SELECT value FROM danmaku_source_checkpoint').fetchone()[0],'original')
            with self.assertRaises(ValueError):migration.drain_table(source,cold,'event')

    def test_copy_verify_and_detach_preserve_event(self):
        with tempfile.TemporaryDirectory() as d:
            source=Path(d)/'live.db';cold=Path(d)/'cold.db';report=Path(d)/'report.json'
            with sqlite3.connect(source) as c:
                c.executescript(store.SCHEMA)
                c.execute('CREATE TABLE event(id INTEGER PRIMARY KEY,content TEXT)')
                c.execute("INSERT INTO event VALUES(1,'unchanged')")
                aid=store.artifact(c,'replay','one',b'raw bytes')
                store.add_records(c,[store.record('replay','one','s',1,2,'user','text',1000,artifact_id=aid)])
                c.execute("INSERT INTO danmaku_source_link VALUES(1,1,'test')")
            with patch.object(store,'PRODUCTION_DB',source),patch.object(store,'ARCHIVE_DB',cold),patch.object(migration,'REPORT',report),contextlib.redirect_stdout(io.StringIO()):
                migration.copy()
                migration.detach()
            with sqlite3.connect(source) as c:
                self.assertEqual(c.execute('SELECT * FROM event').fetchall(),[(1,'unchanged')])
                self.assertEqual(c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'danmaku_%'").fetchone()[0],0)
            with sqlite3.connect(cold) as c:
                self.assertEqual(c.execute('SELECT source,content FROM danmaku_source_record').fetchall(),[('replay','text')])
            self.assertTrue(json.loads(report.read_text())['detached'])


if __name__=='__main__':unittest.main()
