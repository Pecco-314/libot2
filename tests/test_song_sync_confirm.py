import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from scripts.sync_song_list_confirm import snapshot, preview, apply
from src.db.song_list import init_song_list_db


class SongSyncConfirmTest(unittest.TestCase):
    def test_preserves_local_edits_and_new_dates_and_refuses_stale_plan(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'songs.db'; init_song_list_db(path)
            with sqlite3.connect(path) as conn:
                conn.row_factory=sqlite3.Row
                conn.execute("INSERT INTO song_info(id,title,original_singer,lyrics,lyrics_cleaned) VALUES(1,'歌','本地','歌词','歌词')")
                conn.executemany('INSERT INTO song_record(song_id,record_date) VALUES(1,?)', [('2026-09-01',),('2026-09-08',)])
                conn.commit()
                songs=[dict(external_id='9',title='歌',title_trans='',original_singer='网站',notes='',language='',clips=[],tags='',records=['2026-09-01','2026-09-01'])]
                p=preview(snapshot(conn),songs)
                self.assertFalse(next(c for c in p['changes'] if c['kind']=='field')['selected'])
                apply(conn,p,Path(d)/'backup.json')
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM song_record').fetchone()[0],3)
                self.assertEqual(conn.execute('SELECT original_singer,lyrics FROM song_info').fetchone()[:],('本地','歌词'))
                with self.assertRaises(ValueError): apply(conn,p,Path(d)/'stale.json')
                self.assertFalse((Path(d)/'stale.json').exists())
                self.assertEqual(len(json.loads((Path(d)/'backup.json').read_text())['song_record']),2)

    def test_disambiguates_singer_and_does_not_infer_by_dates(self):
        data={'song_info':[dict(id=1,title='同名',title_trans='',original_singer='甲',notes='',language='',clips='',tags=''),
                           dict(id=2,title='同名',title_trans='',original_singer='乙',notes='',language='',clips='',tags='')], 'song_record':[]}
        remote=dict(external_id='1',title='同名',title_trans='',original_singer='乙',notes='',language='',clips=[],tags='',records=['2026-09-01'])
        p=preview(data,[remote]); self.assertEqual(p['changes'][0]['song_id'],2)
        remote['title']='不同歌'; p=preview(data,[remote])
        self.assertFalse(p['changes'][0]['selected'])
        self.assertEqual(p['changes'][0]['kind'],'new_song')
