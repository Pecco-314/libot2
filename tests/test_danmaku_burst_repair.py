import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import repair_danmaku_clock_bursts as repair
from scripts.danmaku_clock_policy import apply_policy
from scripts.build_historical_source_database import SCHEMA
from scripts.backfill_mit3uri_replay_danmaku import PROVENANCE_TABLE_SQL
from src.db.event_search import init_event_search_db, index_pending_events


class OccurrenceTests(unittest.TestCase):
    def test_same_source_repeated_messages_preserve_multiplicity(self):
        source=[dict(id=i,uid=i%12+1,content=f'独立校准消息{i:04d}',time=600+i/10) for i in range(80)]
        ref=[dict(r,id=r['id']+1000,time=r['id']*3) for r in source]
        # Two source occurrences versus one reference occurrence remain two
        # distinct observations after cross-source reconciliation.
        source.extend([dict(id=100+i,uid=900,content='谁家小孩',time=604+i/100) for i in range(2)])
        ref.append(dict(id=2000,uid=900,content='谁家小孩',time=120))
        pairs, validation=repair.pair_occurrences(source,ref,600,610)
        self.assertGreater(validation['within7'],.95)
        repeated=[(a,b) for a,b in pairs if a['uid']==900]
        self.assertEqual(len(repeated),1)
        self.assertEqual(len({b['id'] for a,b in pairs}),len(pairs))

    def test_monotone_fit_handles_small_packet_order_inversions(self):
        points=repair.monotone_points([(0,100),(1,102),(2,101),(3,103)])
        self.assertEqual(points,[(0,100),(1,101.5),(2,101.5),(3,103)])

    def test_reviewed_policy_survives_reconstruction_without_text_dedup(self):
        original=[dict(id=i,time=100,raw_time=100,source='danmakus',stream='a',uid=1,content='谁家小孩') for i in (1,2,3)]
        normal=dict(original[0],id=4,stream='b',time=50,raw_time=50)
        decisions=[dict(operation='delete',source_id=1,canonical_id=4,timestamp=50),
                   dict(operation='retime',source_id=2,timestamp=52,reference_id=5)]
        actual=apply_policy(original,{'danmakus:a':original,'danmakus:b':[normal]},decisions)
        self.assertEqual({r['id']:r['time'] for r in actual},{2:52,3:100,4:50})
        self.assertEqual(len(actual),3)

    def test_policy_missing_source_fails_closed(self):
        with self.assertRaisesRegex(ValueError,'missing'):
            apply_policy([dict(id=1)],{},[dict(operation='delete',source_id=1,canonical_id=2,timestamp=50)])

    def test_provider_order_brackets_select_correct_repeated_text(self):
        source=[dict(id=100-i,uid=100+i,content=f'独立的长文本消息{i}',time=600,
                     sequence_order=i,provider_order_restored=True) for i in range(10)]
        source[4]['content']='袜套不赖'
        reference=[dict(r,id=1000+i,time=i*i) for i,r in enumerate(source)]
        reference.append(dict(reference[4],id=2000,time=300))
        pairs,report=repair.pair_occurrences(source,reference,590,610)
        self.assertFalse(report['sequence_validated'])
        target=[b for a,b in pairs if a['id']==source[4]['id']]
        self.assertEqual([b['id'] for b in target],[1004])
        self.assertGreater(report['bracket_pairs'],0)

    def test_brackets_require_independent_users(self):
        source=[dict(id=i,uid=i%2+1,content=f'独立的长文本消息{i}',time=600,
                     sequence_order=i,provider_order_restored=True) for i in range(20)]
        reference=[dict(r,id=1000+i,time=i*i) for i,r in enumerate(source)]
        pairs,report=repair.pair_occurrences(source,reference,590,610)
        self.assertFalse(pairs)
        self.assertFalse(report['sequence_validated'])


class RepairApplyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.main=self.root/'main.db';self.search=self.root/'search.db'
        self.ts=repair.CUTOFF-500
        with sqlite3.connect(self.main) as c:
            c.executescript(SCHEMA.replace('id INTEGER PRIMARY KEY,room_id','id INTEGER PRIMARY KEY AUTOINCREMENT,room_id'))
            c.executescript(PROVENANCE_TABLE_SQL)
            for eid,room,cmd,ts,text in [(1,repair.ROOM,'DANMU_MSG',self.ts,'谁家小孩'),(2,repair.ROOM,'DANMU_MSG',self.ts+200,'谁家小孩'),(3,repair.ROOM,'DANMU_MSG',self.ts+200,'校准消息'),(4,repair.ROOM,'LIVE',self.ts,None),(5,repair.ROOM,'DANMU_MSG',repair.CUTOFF,'保护边界'),(6,1,'DANMU_MSG',self.ts,'其他直播间')]:
                c.execute('INSERT INTO event(id,room_id,cmd,uid,content,timestamp) VALUES(?,?,?,1,?,?)',(eid,room,cmd,text,ts))
            c.row_factory=sqlite3.Row
            rows={r['id']:dict(r) for r in c.execute('SELECT * FROM event')}
        insert={k:v for k,v in rows[1].items() if k!='id'}
        insert.update(content='补回消息',timestamp=self.ts+300)
        self.items=[dict(operation='delete',before=rows[2],keep=rows[1],provenance=[]),
                    dict(operation='retime',before=rows[3],after={**rows[3],'timestamp':self.ts},provenance=[]),
                    dict(operation='insert',after=insert,source_record_id=100)]
        path=self.root/'changes.jsonl'
        path.write_text(''.join(json.dumps(x)+'\n' for x in self.items))
        (self.root/'report.json').write_text(json.dumps(dict(proposal_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),operations=dict(delete=1,retime=1,insert=1))))
        init_event_search_db(self.search);index_pending_events(self.main,self.search)

    def tearDown(self):
        self.temp.cleanup()

    def test_apply_verify_and_idempotent_resume(self):
        repair.apply_plan(self.root,self.main,self.search)
        repair.apply_plan(self.root,self.main,self.search)
        with sqlite3.connect(self.main) as c:
            self.assertEqual(c.execute('SELECT id FROM event ORDER BY id').fetchall(),[(1,),(3,),(4,),(5,),(6,),(7,)])
            self.assertEqual(c.execute('SELECT timestamp FROM event WHERE id=3').fetchone()[0],self.ts)
        self.assertTrue(json.loads((self.root/'execution.json').read_text())['verified'])

    def test_resume_after_main_commit_and_before_search_repair(self):
        with patch.object(repair,'repair_search',side_effect=RuntimeError('interrupted')):
            with self.assertRaisesRegex(RuntimeError,'interrupted'):
                repair.apply_plan(self.root,self.main,self.search)
        repair.apply_plan(self.root,self.main,self.search)
        self.assertTrue(json.loads((self.root/'execution.json').read_text())['verified'])

    def test_snapshot_change_aborts_transaction(self):
        with sqlite3.connect(self.main) as c:
            c.execute("UPDATE event SET content='新内容' WHERE id=3")
        with self.assertRaisesRegex(ValueError,'Snapshot'):
            repair.apply_plan(self.root,self.main,self.search)
        with sqlite3.connect(self.main) as c:
            self.assertIsNotNone(c.execute('SELECT 1 FROM event WHERE id=2').fetchone())

    def test_scope_guards(self):
        with sqlite3.connect(self.main) as c:
            repair.install_guards(c)
            for eid in (4,5,6):
                with self.assertRaisesRegex(sqlite3.IntegrityError,'protected'):
                    c.execute('DELETE FROM event WHERE id=?',(eid,))
            with self.assertRaisesRegex(sqlite3.IntegrityError,'protected'):
                c.execute('UPDATE event SET timestamp=? WHERE id=1',(repair.CUTOFF,))


if __name__=='__main__':
    unittest.main()
