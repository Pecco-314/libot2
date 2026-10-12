import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.common.group_history import history_row, recover_group_history
from src.db import recall


NOW = 1800000000


def message(mid, age=0, group=1):
    return dict(message_type='group',group_id=group,message_id=mid,real_seq=str(mid),
                time=NOW-age,user_id=123,sender={'card':'群名片','nickname':'昵称'},
                message=[{'type':'text','data':{'text':f'内容{mid}'}}])


class GroupHistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name)/'cache.db'
        self.db_patch = patch.object(recall,'DB_PATH',self.path)
        self.db_patch.start()
        recall.init_db()

    def tearDown(self):
        self.db_patch.stop()
        self.directory.cleanup()

    def rows(self):
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute('SELECT * FROM message ORDER BY message_id')]

    async def test_reverse_paging_deduplicates_and_stops_at_time_boundary(self):
        api = AsyncMock(side_effect=[
            {'messages':[message(3,10),message(2,20)]},
            {'messages':[message(2,20),message(1,100),message(0,300)]},
        ])
        result = await recover_group_history(api,1,NOW-200,cache_path=self.path,now=NOW,delay=0)
        self.assertTrue(result['complete'])
        self.assertEqual(result['inserted'],3)
        self.assertEqual([r['message_id'] for r in self.rows()],[1,2,3])
        self.assertTrue(api.await_args.kwargs['reverse_order'])
        self.assertEqual(api.await_args.kwargs['message_seq'],'2')

    async def test_realtime_cache_and_recall_tombstone_are_not_overwritten(self):
        recall.save_message(1,1,123,NOW-100,'实时昵称',[{'type':'image','file':'downloaded.img'}])
        recall.mark_recalled(1,2,123,NOW-50,456)
        api = AsyncMock(return_value={'messages':[message(2,50),message(1,100),message(0,300)]})
        result = await recover_group_history(api,1,NOW-200,cache_path=self.path,now=NOW,delay=0)
        self.assertEqual(result['inserted'],0)
        rows = self.rows()
        self.assertEqual(rows[0]['nickname'],'实时昵称')
        self.assertIn('downloaded.img',rows[0]['segments'])
        self.assertEqual(rows[1]['recalled_at'],NOW-50)
        self.assertIsNone(rows[1]['segments'])

    async def test_duplicate_page_returns_partial_status_and_second_run_is_idempotent(self):
        api = AsyncMock(return_value={'messages':[message(3,10),message(2,20)]})
        first = await recover_group_history(api,1,NOW-200,cache_path=self.path,now=NOW,delay=0)
        self.assertFalse(first['complete'])
        self.assertEqual(first['inserted'],2)
        self.assertEqual(api.await_count,2)
        second = await recover_group_history(api,1,NOW-200,cache_path=self.path,now=NOW,delay=0)
        self.assertEqual(second['inserted'],0)

    async def test_page_cap_does_not_claim_complete(self):
        api = AsyncMock(return_value={'messages':[message(1,10)]})
        result = await recover_group_history(api,1,NOW-200,cache_path=self.path,now=NOW,max_pages=1,delay=0)
        self.assertFalse(result['complete'])

    def test_images_and_forwards_are_placeholders_without_url_downloads(self):
        msg = message(1,10)
        msg['message'] = [
            {'type':'image','data':{'url':'https://example.com/image'}},
            {'type':'at','data':{'qq':'456'}},
            {'type':'forward','data':{'id':'123'}},
        ]
        row = history_row(msg,1,NOW-200,NOW+1)
        self.assertEqual(row['nickname'],'群名片')
        self.assertEqual(row['segments'],[{'type':'image','file':''},
                         {'type':'text','text':'@456'},{'type':'text','text':'[forward]'}])

    def test_wrong_group_invalid_messages_and_old_messages_are_excluded(self):
        self.assertIsNone(history_row(message(1,10,group=2),1,NOW-200,NOW+1))
        self.assertIsNone(history_row(message(1,300),1,NOW-200,NOW+1))
        self.assertIsNone(history_row({'time':None},1,NOW-200,NOW+1))
