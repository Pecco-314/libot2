import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import build_historical_source_database as build
from scripts.normalize_historical_sc import clusters
from scripts.replace_historical_source_database import ROOM_ID


def sc(eid,ts,text='晚安',price=30):
    return dict(id=eid,room_id=ROOM_ID,cmd='SUPER_CHAT_MESSAGE',uid=1,content=text,total_coin=price,timestamp=ts)


class HistoricalScNormalizationTests(unittest.TestCase):
    def test_same_identity_within_minute(self):
        self.assertEqual(len(clusters([sc(1,100),sc(2,100.4),sc(3,122)])),1)

    def test_different_text_price_or_outside_minute_preserved(self):
        self.assertEqual(clusters([sc(1,100),sc(2,120,text='点歌'),sc(3,130,price=50),sc(4,161)]),[])

    def test_no_transitive_chain_beyond_minute(self):
        self.assertEqual([[r['id'] for r in x] for x in clusters([sc(1,100),sc(2,150),sc(3,200)])],[[1,2]])

    def test_ordinary_danmaku_rejected(self):
        with self.assertRaises(ValueError):clusters([{**sc(1,100),'cmd':'DANMU_MSG'}])

    def test_builder_keeps_evidence_but_coalesces_sc(self):
        with sqlite3.connect(':memory:') as out,sqlite3.connect(':memory:') as cold:
            out.executescript(build.SCHEMA)
            base=dict(provider='danmakus',stream='one/full',kind='sc',uid=1,uname='测试',ts=100.2,text='晚安',num=1,coin=30)
            records=[{**base,'key':'1'},{**base,'key':'2','ts':100.4}]
            with patch.object(build,'native_rows',return_value=iter(records)),patch.object(build,'load_source',return_value=[]):
                build.build_finance(out,Path('/unused'))
            build.finalize_evidence(out,cold)
            self.assertEqual(out.execute('SELECT COUNT(*) FROM event').fetchone()[0],1)
            self.assertEqual(out.execute('SELECT COUNT(*) FROM financial_evidence').fetchone()[0],2)
            self.assertEqual(out.execute('SELECT COUNT(*) FROM sc_signal_merge').fetchone()[0],1)
            build.finalize_evidence(out,cold)
            self.assertEqual(out.execute('SELECT COUNT(*) FROM event').fetchone()[0],1)


if __name__=='__main__':unittest.main()
