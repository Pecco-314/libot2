import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import build_historical_source_database as build


def dm(i,t,stream='a',uid=1):
    return dict(id=i,uid=uid,uname='测试',content='晚安',source='danmakus',stream=stream,time=t,raw_time=t,clock_basis='native_utc',run_start=120 if stream=='b' else 0,run_end=240 if stream=='b' else 120)


class SourceRebuildTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');self.db.executescript(build.SCHEMA)

    def tearDown(self):self.db.close()

    def test_identical_same_source_occurrences_are_preserved(self):
        for i in (1,2):self.assertTrue(build.insert_danmaku(self.db,dm(i,100),'s'))
        self.assertFalse(build.insert_danmaku(self.db,dm(1,100),'s'))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM event').fetchone()[0],2)

    def test_boundary_consumes_at_most_one_per_stream(self):
        removed,ambiguous=build.boundary_duplicates([dm(1,119),dm(2,120,'b'),dm(3,121,'b')])
        self.assertEqual(removed,{2});self.assertEqual(ambiguous,0)

    def test_unknown_uid_never_identity_deduplicated(self):
        removed,_=build.boundary_duplicates([dm(1,119,uid=None),dm(2,120,'b',uid=None)])
        self.assertFalse(removed)

    def test_protected_period_rejected(self):
        with self.assertRaises(ValueError):build.insert_danmaku(self.db,dm(1,build.store.EXTERNAL_CUTOFF_MS/1000),'s')

    def test_gap_selects_one_source_not_union(self):
        streams={'danmakus:a':[dm(i,10+i) for i in range(20)],'vtbcat:b':[dm(30+i,130+i) for i in range(20)]}
        primary,runs=build.select_cells(streams,0,240)
        self.assertEqual(len(runs),2)
        self.assertEqual([r['source'] for r in runs],['danmakus:a','vtbcat:b'])

    def test_financial_stream_multiplicity_preserved(self):
        base=dict(provider='danmakus',stream='one/full',kind='gift',uid=1,uname='测试',ts=100,text='花',num=1,coin=100)
        rows=[{**base,'key':'1'},{**base,'key':'2'},{**base,'provider':'vtbcat','stream':'two','key':'3'}]
        with patch.object(build,'native_rows',return_value=iter(rows)),patch.object(build,'load_source',return_value=[]):build.build_finance(self.db,Path('/unused'))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM event').fetchone()[0],2)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM financial_evidence').fetchone()[0],3)

    def test_qqzy_sc_is_included_with_source_attribution(self):
        qq=[dict(month='2025-11',source_index=0,timestamp=100,uid=2,uname='某人',content='生日快乐',price=30)]
        with patch.object(build,'native_rows',return_value=iter([])),patch.object(build,'load_source',return_value=qq):build.build_finance(self.db,Path('/unused'))
        self.assertEqual(self.db.execute('SELECT cmd,total_coin FROM event').fetchone(),('SUPER_CHAT_MESSAGE',30))
        self.assertEqual(self.db.execute('SELECT source FROM origin').fetchone()[0],'qqzy')

    def test_financial_match_prefers_same_quantity_over_closer_time(self):
        base=dict(provider='danmakus',stream='one/full',kind='gift',uid=1,uname='测试',ts=100,text='花',num=1,coin=100)
        rows=[{**base,'key':'1'}, {**base,'key':'2','ts':101,'num':10,'coin':1000},
              {**base,'provider':'vtbcat','stream':'two','key':'3','ts':100,'num':10,'coin':1000},
              {**base,'provider':'vtbcat','stream':'two','key':'4','ts':101}]
        with patch.object(build,'native_rows',return_value=iter(rows)),patch.object(build,'load_source',return_value=[]):build.build_finance(self.db,Path('/unused'))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM event').fetchone()[0],2)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM review').fetchone()[0],0)

    def test_sc_millisecond_precision_preserves_same_second_occurrences(self):
        base=dict(provider='danmakus',stream='one/full',kind='sc',uid=1,uname='测试',ts=100.2,text='晚安',num=1,coin=30)
        rows=[{**base,'key':'1'},{**base,'key':'2','ts':100.4}]
        with patch.object(build,'native_rows',return_value=iter(rows)),patch.object(build,'load_source',return_value=[]):build.build_finance(self.db,Path('/unused'))
        self.db.execute("CREATE UNIQUE INDEX sc_unique ON event(room_id,uid,content,total_coin,timestamp) WHERE cmd='SUPER_CHAT_MESSAGE'")
        self.assertEqual(self.db.execute('SELECT timestamp FROM event ORDER BY id').fetchall(),[(100.2,),(100.4,)])


if __name__=='__main__':unittest.main()
