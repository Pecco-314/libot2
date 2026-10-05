import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import build_historical_source_database as build
from scripts import replace_historical_source_database as replace
from scripts.backfill_mit3uri_replay_danmaku import PROVENANCE_TABLE_SQL
from src.db.event_search import init_event_search_db,index_pending_events


class HistoricalReplacementTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.main=self.root/'main.sqlite3';self.search=self.root/'search.sqlite3'
        self.candidate=self.root/'candidate';self.candidate.mkdir();self.work=self.root/'replacement'
        self.ts=replace.CUTOFF-1000
        with sqlite3.connect(self.main) as c:
            c.executescript(build.SCHEMA.replace('id INTEGER PRIMARY KEY,room_id','id INTEGER PRIMARY KEY AUTOINCREMENT,room_id'))
            c.executescript(PROVENANCE_TABLE_SQL)
            c.execute("CREATE UNIQUE INDEX idx_event_sc_unique ON event(room_id,uid,content,total_coin,timestamp) WHERE cmd='SUPER_CHAT_MESSAGE'")
            for eid,room,cmd,ts,text in [(10,replace.ROOM_ID,'DANMU_MSG',self.ts,'旧晚安'),(11,replace.ROOM_ID,'DANMU_MSG',self.ts,'旧晚安'),(12,replace.ROOM_ID,'LIVE',self.ts,None),(13,replace.ROOM_ID,'DANMU_MSG',replace.CUTOFF,'保护边界'),(14,99,'DANMU_MSG',self.ts,'其他房间')]:
                c.execute('INSERT INTO event(id,room_id,cmd,uid,content,timestamp) VALUES(?,?,?,1,?,?)',(eid,room,cmd,text,ts))
        with sqlite3.connect(self.candidate/'rebuilt.sqlite3') as c:
            c.executescript(build.SCHEMA)
            for eid in (1,2):c.execute("INSERT INTO event(id,room_id,cmd,uid,content,timestamp) VALUES(?,?,'DANMU_MSG',1,'重建晚安',?)",(eid,replace.ROOM_ID,self.ts))
            c.execute('CREATE TABLE replay_provenance(event_id INTEGER PRIMARY KEY,payload TEXT)')
            p=['bilibili_official_replay',123,'456','s','BVtest',0,0,'abc',self.ts,self.ts*1000,'source_rebuild:test','inserted',1]
            c.execute('INSERT INTO replay_provenance VALUES(1,?)',(json.dumps(p),))
        (self.candidate/'audit.json').write_text(json.dumps({'ready_for_promotion':True}))
        init_event_search_db(self.search);index_pending_events(self.main,self.search)

    def tearDown(self):self.temp.cleanup()

    def prepare(self):replace.prepare(self.candidate,self.work,self.main,target=4)

    def test_replaces_whole_range_preserving_status_other_rooms_and_boundary(self):
        self.prepare();replace.apply(self.candidate,self.work,self.main,self.search)
        replace.verify(self.candidate,self.work,self.main,self.search)
        with sqlite3.connect(self.main) as c:
            self.assertEqual(c.execute('SELECT id FROM event WHERE id<15 ORDER BY id').fetchall(),[(12,),(13,),(14,)])
            self.assertEqual(c.execute("SELECT COUNT(*) FROM event WHERE content='重建晚安'").fetchone()[0],2)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM mit3uri_replay_danmaku_provenance').fetchone()[0],1)
        replace.apply(self.candidate,self.work,self.main,self.search)
        with sqlite3.connect(self.main) as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM event').fetchone()[0],5)

    def test_resume_after_main_commit_before_search_and_external_receipt(self):
        self.prepare()
        with patch.object(replace,'prune_search',side_effect=RuntimeError('simulated crash')):
            with self.assertRaisesRegex(RuntimeError,'simulated crash'):replace.apply(self.candidate,self.work,self.main,self.search)
        replace.apply(self.candidate,self.work,self.main,self.search)
        replace.verify(self.candidate,self.work,self.main,self.search)

    def test_changed_snapshot_aborts_without_replacing(self):
        self.prepare()
        with sqlite3.connect(self.main) as c:c.execute("UPDATE event SET content='外部修改' WHERE id=10")
        with self.assertRaisesRegex(ValueError,'snapshot differs'):replace.apply(self.candidate,self.work,self.main,self.search)
        with sqlite3.connect(self.main) as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM event').fetchone()[0],5)

    def test_scope_guard_forbids_status_foreign_room_and_cutoff(self):
        with sqlite3.connect(self.main) as c:
            replace.install_scope_guards(c)
            for eid in (12,13,14):
                with self.assertRaisesRegex(sqlite3.IntegrityError,'protected event'):
                    c.execute('DELETE FROM event WHERE id=?',(eid,))
            with self.assertRaisesRegex(sqlite3.IntegrityError,'protected event'):
                c.execute('UPDATE event SET timestamp=? WHERE id=10',(replace.CUTOFF,))


if __name__=='__main__':unittest.main()
