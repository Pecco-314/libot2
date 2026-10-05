import sqlite3
import unittest
import msgpack

from scripts.apply_danmaku_segment_cleanup import apply_batch,validate
from scripts.backfill_mit3uri_replay_danmaku import ROOM_ID
from src.db.danmaku_sources import EXTERNAL_CUTOFF_MS
from scripts.prune_danmaku_cleanup_search import prune_batch
from src.db.event_search import _document_tokens,_encode_rowid
from scripts.audit_archived_financial_events import packed_inventory


class SegmentCleanupTest(unittest.TestCase):
    def test_financial_inventory_respects_cutoff_and_retains_raw_prices(self):
        packed=msgpack.packb([[None,None,ROOM_ID],[],[EXTERNAL_CUTOFF_MS-2000,[],[],[
            [0,1,1,0,3,['gift',1,100]],
            [500,2,1,0,4,['舰长',1,138000]],
            [500,3,1,0,5,['message',30000]],
            [1000,1,1,0,3,['post cutoff',1,100]],
        ]]],use_bin_type=True)
        counts,samples=packed_inventory(packed)
        self.assertEqual(dict(counts),{'gift':1,'guard':1,'sc':1})
        self.assertEqual(samples['guard']['payload'][2],138000)

    def setUp(self):
        self.conn=sqlite3.connect(':memory:');self.conn.row_factory=sqlite3.Row
        self.conn.execute('CREATE TABLE event(id INTEGER PRIMARY KEY,room_id INTEGER,cmd TEXT,uid INTEGER,content TEXT,timestamp INTEGER)')
        self.conn.execute('CREATE TABLE mit3uri_replay_danmaku_provenance(event_id INTEGER,match_kind TEXT)')
        self.conn.executemany('INSERT INTO event VALUES(?,?,?,?,?,?)',[(1,ROOM_ID,'DANMU_MSG',9,'晚安',123),(2,ROOM_ID,'DANMU_MSG',9,'晚安',124),(3,ROOM_ID,'SEND_GIFT',9,'礼物',125)])
        self.conn.execute("INSERT INTO mit3uri_replay_danmaku_provenance VALUES(2,'inserted')");self.conn.commit()
        self.item=dict(selected_primary='danmakus:one',keep_event=dict(self.conn.execute('SELECT * FROM event WHERE id=1').fetchone()),remove_event=dict(self.conn.execute('SELECT * FROM event WHERE id=2').fetchone()),remove_provenance=[dict(event_id=2,match_kind='inserted')])

    def tearDown(self):self.conn.close()

    def test_exact_deletion_and_resume_leave_gift_unchanged(self):
        self.assertEqual(apply_batch(self.conn,[self.item]),([2],[]))
        self.assertEqual(apply_batch(self.conn,[self.item]),([],[2]))
        self.assertEqual([r[0] for r in self.conn.execute('SELECT id FROM event ORDER BY id')],[1,3])

    def test_changed_keep_refuses_deletion(self):
        self.conn.execute("UPDATE event SET content='不同' WHERE id=1");self.conn.commit()
        with self.assertRaises(RuntimeError):apply_batch(self.conn,[self.item])
        self.assertIsNotNone(self.conn.execute('SELECT * FROM event WHERE id=2').fetchone())

    def test_post_cutoff_is_rejected(self):
        self.item['remove_event']['timestamp']=EXTERNAL_CUTOFF_MS//1000
        with self.assertRaises(ValueError):validate(self.item)

    def test_changed_provenance_rolls_back(self):
        self.conn.execute("UPDATE mit3uri_replay_danmaku_provenance SET match_kind='existing'");self.conn.commit()
        with self.assertRaises(RuntimeError):apply_batch(self.conn,[self.item])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM event').fetchone()[0],3)

    def test_search_prune_preserves_other_document(self):
        c=self.conn
        c.execute("CREATE VIRTUAL TABLE event_fts USING fts5(tokens,content='',detail=none,columnsize=0)")
        c.execute('CREATE TABLE event_search_meta(key TEXT PRIMARY KEY,value TEXT)')
        c.executemany('INSERT INTO event_search_meta VALUES(?,?)',[('last_indexed_event_id','3'),('indexed_document_count','2')])
        for row in (self.item['keep_event'],self.item['remove_event']):
            c.execute('INSERT INTO event_fts(rowid,tokens) VALUES(?,?)',(_encode_rowid(row['timestamp'],row['id']),_document_tokens(row['room_id'],row['content'])))
        c.commit()
        self.assertEqual(prune_batch(c,[self.item['remove_event']],'cleanup:test',1),1)
        result=c.execute('SELECT rowid FROM event_fts WHERE event_fts MATCH ?',('r'+str(ROOM_ID),)).fetchall()
        self.assertEqual([r[0] for r in result],[_encode_rowid(123,1)])
        self.assertEqual(c.execute("SELECT value FROM event_search_meta WHERE key='indexed_document_count'").fetchone()[0],'1')


if __name__=='__main__':unittest.main()
