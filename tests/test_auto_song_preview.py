import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from src.common.auto_song_preview import AutoSongPreview, PreviewQueue, configured_target


class AutoPreviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.queue = PreviewQueue(Path(self.temp.name)/'queue.db')
        self.worker = AutoSongPreview(self.queue)
        self.bot = Mock(self_id='123', call_api=AsyncMock(return_value={'message_id': 456}))
        self.session = {'start_ts': 900, 'end_ts': 1100}
        self.queue.discover(1, 2, [], 1000)
        self.result = {'has_asr': True, 'pending': False,
                       'entries': [{'title': '歌曲', 'singer': '歌手', 'first_ts': 950}]}

    def test_delay_idempotency_restart_and_historical_guard(self):
        self.assertEqual(self.queue.discover(1, 2, [self.session], 1200), [])
        jobs = self.queue.discover(1, 2, [self.session, self.session], 1300)
        self.assertEqual(len(jobs), 1)
        self.queue.update(jobs[0], done=1)
        fresh = PreviewQueue(self.queue.path)
        self.assertEqual(fresh.discover(1, 2, [self.session], 1400), [])
        self.assertEqual(fresh.discover(1, 3, [self.session], 1400), [])

    async def test_delivery_uses_default_preview_and_mentions_all_after_forward(self):
        with patch('src.common.auto_song_preview.list_live_sessions', return_value=[self.session]), \
             patch('src.common.auto_song_preview.preview_cache.get', return_value=self.result) as generate:
            await self.worker.run(self.bot, 1, 2, now=1300)
            generate.assert_called_once_with(1, 900, 1100, .85, now=1300)
            await self.worker.run(self.bot, 1, 2, now=1400)
        calls = self.bot.call_api.await_args_list
        self.assertEqual([c.args[0] for c in calls], ['send_group_forward_msg', 'send_group_msg'])
        self.assertTrue(all(c.kwargs['group_id'] == 2 for c in calls))
        self.assertEqual(calls[-1].kwargs['message'][0].data['qq'], 'all')

    async def test_mention_failure_completes_without_retrying_next_day(self):
        jobs = self.queue.discover(1, 2, [self.session], 1300)
        self.bot.call_api.side_effect = [{'message_id': 1}, RuntimeError('at all failed')]
        with patch('src.common.auto_song_preview.preview_cache.get', return_value=self.result):
            await self.worker.deliver(self.bot, jobs[0], 1300)
        fresh = PreviewQueue(self.queue.path)
        self.assertEqual(fresh.discover(1, 2, [self.session], 1400), [])
        self.assertEqual(fresh.discover(1, 2, [self.session], 86400), [])
        self.assertEqual(self.bot.call_api.await_count, 2)

    def test_ongoing_session_does_not_trigger_at_midnight(self):
        self.assertEqual(self.queue.discover(1, 2, [{'start_ts': 2000, 'end_ts': None}], 86400), [])

    async def test_offline_jobs_survive(self):
        with patch('src.common.auto_song_preview.list_live_sessions', return_value=[self.session]):
            await self.worker.run(None, 1, 2, now=1300)
        self.assertEqual(len(self.queue.discover(1, 2, [], 1400)), 1)

    async def test_no_songs_does_not_ping_group(self):
        job = self.queue.discover(1, 2, [self.session], 1300)[0]
        with patch('src.common.auto_song_preview.preview_cache.get', return_value={**self.result, 'entries': []}):
            await self.worker.deliver(self.bot, job, 1300)
        self.bot.call_api.assert_not_called()
        self.assertEqual(self.queue.discover(1, 2, [], 1400), [])

    async def test_missing_asr_waits_before_abandoning(self):
        job = self.queue.discover(1, 2, [self.session], 1300)[0]
        with patch('src.common.auto_song_preview.preview_cache.get', return_value={'has_asr': False, 'pending': False}):
            with self.assertRaises(RuntimeError):
                await self.worker.deliver(self.bot, job, 1300)
            await self.worker.deliver(self.bot, job, 3000)
        self.bot.call_api.assert_not_called()

    def test_empty_group_disables_feature(self):
        with patch.dict('os.environ', {'SONG_PREVIEW_TEST_GROUP': ''}):
            self.assertIsNone(configured_target())
        with patch.dict('os.environ', {'SONG_PREVIEW_TEST_GROUP': '2', 'SONG_PREVIEW_AUTO_ROOM': '1'}):
            self.assertEqual(configured_target(), (1, 2))


if __name__ == '__main__':
    unittest.main()
