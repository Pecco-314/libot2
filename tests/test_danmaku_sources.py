import json
import sqlite3
import unittest
import zlib
import csv
import tempfile
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch

from src.db import danmaku_sources as s
from scripts.archive_danmaku_sources import archive_page
from scripts import archive_danmaku_sources as archive
from scripts.reconcile_danmaku_archive import remove_video_comments, enforce_policy, ROOM_ID


class SourceArchiveTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        s.initialize(self.db)
        self.db.execute('CREATE TABLE event(id INTEGER PRIMARY KEY, cmd TEXT)')
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_cold_connection_cannot_write_event(self):
        with tempfile.TemporaryDirectory() as d:
            main=Path(d)/'live.db';cold=Path(d)/'cold.db'
            with sqlite3.connect(main) as c:
                c.execute('CREATE TABLE event(id INTEGER PRIMARY KEY,content TEXT)')
                c.execute("INSERT INTO event VALUES(1,'keep')")
            with patch.object(s,'PRODUCTION_DB',main),patch.object(s,'ARCHIVE_DB',cold):
                c=s.connect_archive()
                try:
                    with c:s.initialize(c)
                    self.assertEqual(c.execute('SELECT content FROM event').fetchone()[0],'keep')
                    with self.assertRaises(sqlite3.OperationalError):
                        c.execute('DELETE FROM event')
                finally:c.close()
                with sqlite3.connect(main) as c:
                    with self.assertRaises(ValueError):s.initialize(c)

    def test_repeat_messages_and_resume(self):
        rows = [s.record('replay', i, 's', 1, 2, 'u', '晚安', 1000,
                         mapped=2000, eligibility='video_comment') for i in (1, 2)]
        self.assertEqual(archive_page(self.db, 'replay', 'a', b'raw', {}, rows), 2)
        self.assertEqual(archive_page(self.db, 'replay', 'a', b'raw', {}, rows), 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM danmaku_source_record').fetchone()[0], 2)
        self.assertEqual(self.db.execute('SELECT source_timestamp_ms,mapped_timestamp_ms,eligibility FROM danmaku_source_record LIMIT 1').fetchone(), (1000, 2000, 'video_comment'))
        self.assertEqual(zlib.decompress(self.db.execute('SELECT payload FROM danmaku_source_artifact').fetchone()[0]), b'raw')

    def test_policy_uses_mapped_replay_time(self):
        cutoff = int(datetime(2026,4,20,17,30,37,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()*1000)
        self.assertEqual(s.EXTERNAL_CUTOFF_MS, cutoff)
        with self.db:
            s.add_records(self.db, [
                s.record('replay', 1, 's', ROOM_ID, 2, 'u', 'a', cutoff+1000, mapped=cutoff-1),
                s.record('replay', 2, 's', ROOM_ID, 2, 'u', 'a', cutoff-1000, mapped=cutoff),
                s.record('vtbcat', 1, 's', ROOM_ID, 2, 'u', 'a', cutoff),
                s.record('monitor', 1, '', ROOM_ID, 2, 'u', 'a', cutoff),
            ])
        self.assertEqual(enforce_policy(self.db)['outside_policy'], 2)
        self.assertEqual([r[0] for r in self.db.execute('SELECT eligibility FROM danmaku_source_record ORDER BY id')],
                         ['live','outside_policy','outside_policy','live'])

    def test_first_live_boundary_retracts_only_inferred_monitor_origin(self):
        from scripts.repair_danmaku_monitor_boundary import repair,OLD_CUTOFF
        with self.db:
            s.add_records(self.db,[
                s.record('monitor','early','',ROOM_ID,2,'u','a',OLD_CUTOFF+1000,evidence={'attribution':'user_confirmed_monitor_since_2026_04_20'}),
                s.record('monitor','live','',ROOM_ID,2,'u','a',s.EXTERNAL_CUTOFF_MS,evidence={'attribution':'user_confirmed_monitor_since_2026_04_20'}),
                s.record('vtbcat','early','x',ROOM_ID,2,'u','a',OLD_CUTOFF+1000,eligibility='outside_policy'),
            ])
        result=repair(self.db)
        self.assertEqual(result['monitor_to_unknown'],1)
        self.assertEqual(result['vtbcat_reeligible'],1)
        self.assertEqual(self.db.execute("SELECT source FROM danmaku_source_record WHERE source_key='early' AND source!='vtbcat'").fetchone()[0],'legacy_unknown')
        self.assertEqual(self.db.execute("SELECT source FROM danmaku_source_record WHERE source_key='live'").fetchone()[0],'monitor')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM danmaku_policy_revision').fetchone()[0],2)
        self.assertEqual(repair(self.db).get('monitor_to_unknown',0),0)

    def test_monitor_atomic_and_raw(self):
        row = s.CapturedRow((1, 'DANMU_MSG', 2, 'u', '晚安', None, None, None, None, 123), '{"cmd":"DANMU_MSG"}')
        with self.db:
            self.db.executemany('INSERT INTO event VALUES(?,?)', [(1, 'SEND_GIFT'), (2, 'DANMU_MSG')])
            s.archive_monitor_batch(self.db, 0, 2, [(1, 'SEND_GIFT'), row])
        self.assertEqual(self.db.execute('SELECT event_id FROM danmaku_source_link').fetchone()[0], 2)
        raw = zlib.decompress(self.db.execute('SELECT payload FROM danmaku_source_artifact').fetchone()[0])
        self.assertEqual(json.loads(raw), [{'cmd': 'DANMU_MSG'}])
        with self.assertRaises(RuntimeError):
            with self.db:
                self.db.execute("INSERT INTO event VALUES(3,'DANMU_MSG')")
                s.archive_monitor_batch(self.db, 2, 3, [row, row])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM event').fetchone()[0], 2)

    def test_monitor_policy_retains_replay_corroboration(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            (root/'data').mkdir()
            with sqlite3.connect(root/'data/libot.db') as c:
                s.initialize(c)
                c.execute('CREATE TABLE event(id INTEGER PRIMARY KEY, room_id INTEGER, uid INTEGER, uname TEXT, content TEXT, timestamp INTEGER, cmd TEXT)')
                c.execute('CREATE TABLE mit3uri_replay_danmaku_provenance(event_id INTEGER, match_kind TEXT)')
                cutoff=s.EXTERNAL_CUTOFF_MS//1000
                c.executemany("INSERT INTO event VALUES(?,?,2,'u','text',?,'DANMU_MSG')", [(1,ROOM_ID,cutoff),(2,ROOM_ID,cutoff),(3,ROOM_ID,cutoff-1),(4,ROOM_ID,cutoff)])
                c.executemany('INSERT INTO mit3uri_replay_danmaku_provenance VALUES(?,?)',[(1,'existing'),(2,'inserted')])
                for eid,source in [(1,'replay'),(2,'replay'),(4,'danmakus')]:
                    s.add_records(c,[s.record(source,str(eid),'s',ROOM_ID,2,'u','text',cutoff*1000)])
                    c.execute('INSERT INTO danmaku_source_link SELECT id,?,? FROM danmaku_source_record WHERE source=? AND source_key=?',(eid,'test',source,str(eid)))
                c.commit()
                with patch.object(archive,'ROOT',root):
                    self.assertEqual(archive.archive_legacy(c)['inserted'],3)
                    self.assertEqual(archive.archive_legacy(c)['inserted'],0)
                monitor=[r[0] for r in c.execute("SELECT source_key FROM danmaku_source_record WHERE source='monitor' ORDER BY source_key")]
                self.assertEqual(monitor,['event:1','event:4'])
                self.assertEqual(c.execute("SELECT COUNT(*) FROM danmaku_source_record WHERE source='replay'").fetchone()[0],2)

    def test_legacy_snapshot_uses_separate_cold_database(self):
        with tempfile.TemporaryDirectory() as d:
            live=Path(d)/'live.db';cold=Path(d)/'cold.db'
            with sqlite3.connect(live) as c:
                c.execute('CREATE TABLE event(id INTEGER PRIMARY KEY,room_id INTEGER,uid INTEGER,uname TEXT,content TEXT,timestamp INTEGER,cmd TEXT)')
                c.execute('CREATE TABLE mit3uri_replay_danmaku_provenance(event_id INTEGER,match_kind TEXT)')
                c.execute("INSERT INTO event VALUES(1,?,2,'u','text',?,'DANMU_MSG')",(ROOM_ID,s.EXTERNAL_CUTOFF_MS//1000))
            with patch.object(s,'PRODUCTION_DB',live),patch.object(s,'ARCHIVE_DB',cold):
                c=s.connect_archive()
                try:
                    s.initialize(c);c.commit()
                    self.assertEqual(archive.archive_legacy(c)['inserted'],1)
                    self.assertEqual(archive.archive_legacy(c)['inserted'],0)
                    self.assertEqual(c.execute('SELECT COUNT(*) FROM production.event').fetchone()[0],1)
                finally:c.close()

    def test_shared_artifact(self):
        with self.db:
            aid = s.artifact(self.db, 'monitor', 'log', b'log bytes')
        for i in (1, 2):
            archive_page(self.db, 'monitor', str(i), b'log bytes', {},
                         [s.record('monitor', i, '', 1, 2, 'u', 'a', 1)], artifact_id=aid)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM danmaku_source_artifact').fetchone()[0], 1)

    def test_cleanup_state_tracks_restorations_and_consumed_baselines(self):
        with self.db:
            self.db.executemany('INSERT INTO danmaku_removed_event(event_id,reason,event_json,provenance_json) VALUES(?,?,?,?)',[(1,'verified_duplicate:keep=8','{}','[]'),(2,'verified_duplicate:keep=9','{}','[]')])
            self.db.execute('CREATE TABLE danmaku_cleanup_restored_event(event_id INTEGER PRIMARY KEY)')
            self.db.execute('INSERT INTO danmaku_cleanup_restored_event VALUES(1)')
        self.assertEqual(s.cleanup_state(self.db),({2},{9}))
        with self.db:
            self.db.execute('CREATE TABLE danmaku_cleanup_redelete_event(event_id INTEGER,proposal_sha256 TEXT,keep_event_id INTEGER,applied_at TEXT)')
            self.db.execute("INSERT INTO danmaku_cleanup_redelete_event VALUES(1,'new-proof',10,'2026-10-04')")
        self.assertEqual(s.cleanup_state(self.db),({1,2},{9,10}))
        with self.db:
            self.db.execute('ALTER TABLE danmaku_cleanup_restored_event ADD COLUMN restored_at TEXT')
            self.db.execute("UPDATE danmaku_cleanup_restored_event SET restored_at='2026-10-05'")
        self.assertEqual(s.cleanup_state(self.db),({2},{9}))

    def test_video_comment_cleanup_is_recoverable(self):
        c = self.db
        c.execute('DROP TABLE event')
        c.execute('CREATE TABLE event(id INTEGER PRIMARY KEY,cmd TEXT,room_id INTEGER,uid INTEGER,content TEXT)')
        c.execute('CREATE TABLE mit3uri_replay_danmaku_provenance(event_id INTEGER,match_kind TEXT)')
        c.execute("INSERT INTO event VALUES(1,'DANMU_MSG',?,2,'test')", (ROOM_ID,))
        c.execute("INSERT INTO mit3uri_replay_danmaku_provenance VALUES(1,'inserted')")
        aid = s.artifact(c, 'replay', 'pb', b'raw')
        s.add_records(c, [s.record('replay', '10:20', 's', ROOM_ID, 2, 'u', 'test', 1,
                                   eligibility='video_comment', artifact_id=aid)])
        c.execute("INSERT INTO danmaku_source_link VALUES(1,1,'replay_import_inserted')")
        c.commit()
        with tempfile.TemporaryDirectory() as d:
            report = Path(d)/'comments.csv'
            with report.open('w') as f:
                writer = csv.DictWriter(f, fieldnames=['event_id','cid','dm_id','uid','content'])
                writer.writeheader()
                writer.writerow(dict(event_id=1,cid=10,dm_id=20,uid=2,content='test'))
            self.assertEqual(remove_video_comments(c, report), {'validated': 1})
            self.assertEqual(remove_video_comments(c, report, True), {'removed': 1})
            self.assertEqual(remove_video_comments(c, report, True), {'already_removed': 1})
        self.assertEqual(c.execute('SELECT COUNT(*) FROM danmaku_source_record').fetchone()[0], 1)
        backup = json.loads(c.execute('SELECT event_json FROM danmaku_removed_event').fetchone()[0])
        self.assertEqual(backup['content'], 'test')


if __name__ == '__main__':
    unittest.main()
