import sqlite3
import unittest

from scripts.prepare_source_first_changes import reconcile
from scripts.apply_source_first_changes import validate, apply_main, repair_search
from src.db.event_search import _encode_rowid, _document_tokens, _fts_query, SEARCH_SCHEMA_VERSION


def event(eid=10, ts=1745734370):
    return dict(id=eid,room_id=1967216004,cmd='DANMU_MSG',uid=123,uname='测试',content='完整的一句话',gift_name=None,gift_num=None,total_coin=None,title=None,timestamp=ts)


def proposal(op='retime'):
    b=event()
    x=dict(operation=op,before=b,provenance=[],source='danmakus:test',start=b['timestamp']-100,end=b['timestamp']+100)
    if op=='retime':x['after']={**b,'timestamp':b['timestamp']-5}
    return x


class ReconcileTests(unittest.TestCase):
    def rows(self):
        b={**event(), 'time':1745734365.,'raw_time':1745734370.,'aligned':True}
        a=dict(id=20,uid=123,uname='测试',content=b['content'],time=1745734365.,raw_time=1745734365.)
        return a,b

    def test_preserves_id_with_validated_clock(self):
        a,b=self.rows();changes,held,_=reconcile([a],[b],[a],[b],1745734300,1745734500)
        self.assertEqual(changes[0]['operation'],'retime');self.assertEqual(changes[0]['before']['id'],10);self.assertFalse(held)

    def test_holds_uncalibrated_retime(self):
        a,b=self.rows();b['aligned']=False
        changes,held,_=reconcile([a],[b],[a],[b],1745734300,1745734500)
        self.assertFalse(changes);self.assertEqual(held[0]['reason'],'uncalibrated_identity')

    def test_holds_repeated_identity(self):
        a,b=self.rows();b2={**b,'id':11,'timestamp':b['timestamp']+3}
        changes,held,_=reconcile([a],[b,b2],[a],[b,b2],1745734300,1745734500)
        self.assertFalse(changes);self.assertEqual(held[0]['reason'],'repeated_identity')

    def test_holds_neighbor_occurrence(self):
        a,b=self.rows();outside={**b,'timestamp':1745734299}
        changes,held,_=reconcile([a],[],[a],[outside],1745734300,1745734500)
        self.assertFalse(changes);self.assertEqual(held[0]['reason'],'neighbor_occurrence')

    def test_exact_keeps_real_repetitions(self):
        a,b=self.rows();b['timestamp']=int(a['time']);a2={**a,'id':21};b2={**b,'id':11}
        changes,held,kept=reconcile([a,a2],[b,b2],[a,a2],[b,b2],1745734300,1745734500)
        self.assertEqual(kept,2);self.assertFalse(changes);self.assertFalse(held)


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.c=sqlite3.connect(':memory:');self.c.row_factory=sqlite3.Row
        self.c.executescript('CREATE TABLE event(id INTEGER PRIMARY KEY,room_id,cmd,uid,uname,content,gift_name,gift_num,total_coin,title,timestamp); CREATE TABLE mit3uri_replay_danmaku_provenance(event_id,mapped_timestamp_ms,alignment_method);')
        b=event();self.c.execute('INSERT INTO event VALUES('+','.join('?' for _ in b)+')',tuple(b.values()));self.c.commit()

    def tearDown(self):self.c.close()

    def test_retime_only_changes_timestamp(self):
        x=proposal()
        with self.c:apply_main(self.c,[x])
        self.assertEqual(dict(self.c.execute('SELECT * FROM event').fetchone()),x['after'])

    def test_snapshot_change_rolls_back(self):
        x=proposal();x['before']['content']=x['after']['content']='不同内容'
        with self.assertRaises(RuntimeError):
            with self.c:apply_main(self.c,[x])
        self.assertEqual(dict(self.c.execute('SELECT * FROM event').fetchone()),event())

    def test_finance_and_cutoff_rejected(self):
        for field,value in [('cmd','SEND_GIFT'),('timestamp',1776677437),('room_id',1)]:
            x=proposal('delete');x['before'][field]=value
            with self.assertRaises(ValueError):validate(x)

    def test_payload_change_rejected(self):
        x=proposal();x['after']['content']='changed'
        with self.assertRaises(ValueError):validate(x)

    def test_search_retime_and_delete_are_idempotent(self):
        c=sqlite3.connect(':memory:')
        c.executescript("CREATE TABLE event_search_meta(key TEXT PRIMARY KEY,value TEXT); CREATE VIRTUAL TABLE event_fts USING fts5(tokens,content='',detail=none,columnsize=0);")
        c.executemany('INSERT INTO event_search_meta VALUES(?,?)',[('schema_version',str(SEARCH_SCHEMA_VERSION)),('last_indexed_event_id','100'),('indexed_document_count','1')])
        b=event();c.execute('INSERT INTO event_fts(rowid,tokens) VALUES(?,?)',(_encode_rowid(b['timestamp'],b['id']),_document_tokens(b['room_id'],b['content'])));c.commit()
        x=proposal();repair_search(c,[x],'test',1);repair_search(c,[x],'test',1)
        query=_fts_query(b['room_id'],'完整')
        self.assertEqual(c.execute('SELECT rowid FROM event_fts WHERE event_fts MATCH ?',(query,)).fetchall(),[(_encode_rowid(x['after']['timestamp'],10),)])
        d=proposal('delete');d['before']=x['after'];repair_search(c,[d],'test',2);repair_search(c,[d],'test',2)
        self.assertEqual(c.execute('SELECT rowid FROM event_fts WHERE event_fts MATCH ?',(query,)).fetchall(),[])
        self.assertEqual(c.execute("SELECT value FROM event_search_meta WHERE key='indexed_document_count'").fetchone()[0],'0');c.close()


if __name__=='__main__':unittest.main()
