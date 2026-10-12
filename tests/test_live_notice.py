import ast
import asyncio
import logging
import sqlite3
import tempfile
import time
import unittest
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from src.db import live_notice as db
from src.db.event import list_live_notice_events_after
from src.db.sqlite import connect_sqlite


class Fixtures:
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'test.db'
        self.now = int(time.time())
        with connect_sqlite(self.path) as conn:
            conn.executescript("""
                CREATE TABLE event(id INTEGER PRIMARY KEY,cmd TEXT,room_id INTEGER,title TEXT,timestamp INTEGER);
                CREATE INDEX idx_event_cmd_time ON event(cmd,timestamp);
                CREATE TABLE state(key TEXT PRIMARY KEY,value TEXT,updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
                INSERT INTO state(key,value) VALUES('last_event_id','10');
                CREATE TABLE subscription(group_id INTEGER PRIMARY KEY,room_id INTEGER);
                INSERT INTO subscription VALUES(1,100),(2,100);
            """)
        db.init_live_notice_db(self.path)

    def tearDown(self):
        self.directory.cleanup()

    def row(self, event_id=11, cmd='PREPARING', *, age=0, room=100):
        return dict(id=event_id, cmd=cmd, room_id=room, title='直播标题', timestamp=self.now-age)

    def queue(self, *rows):
        db.stage_live_notices(rows, max(r['id'] for r in rows), self.path)

    def records(self):
        with connect_sqlite(self.path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute('SELECT * FROM live_notice_delivery ORDER BY event_id,group_id')]

    def cursor(self):
        with connect_sqlite(self.path) as conn:
            return conn.execute("SELECT value FROM state WHERE key='last_event_id'").fetchone()[0]


class LiveNoticeDBTests(Fixtures, unittest.TestCase):
    def test_queue_survives_cursor_advance_and_repeated_discovery_is_idempotent(self):
        row = self.row()
        self.queue(row)
        self.assertEqual(self.cursor(), '11')
        self.assertEqual(len(db.list_pending_live_notices(self.path)), 2)
        db.mark_live_notice_sent(11, 1, 1234, self.path)
        self.queue(row)
        self.assertEqual(len(self.records()), 2)
        self.assertEqual([(r['group_id'], r['status']) for r in self.records()], [(1, 'sent'), (2, 'pending')])
        self.assertEqual(self.records()[0]['message_id'], 1234)

    def test_enqueue_and_cursor_roll_back_together(self):
        with connect_sqlite(self.path) as conn:
            conn.execute("""CREATE TRIGGER fail_insert BEFORE INSERT ON live_notice_delivery
                         WHEN NEW.event_id=12 BEGIN SELECT RAISE(ABORT,'test failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.queue(self.row(11), self.row(12))
        self.assertEqual(self.cursor(), '10')
        self.assertEqual(self.records(), [])

    def test_middle_events_are_all_read_in_id_order_not_timestamp_order(self):
        events = [self.row(11, 'LIVE'), self.row(12, 'ROOM_CHANGE', age=1), self.row(13)]
        events += [self.row(14, 'DANMU_MSG'), self.row(15, age=db.MAX_NOTICE_AGE+1)]
        with connect_sqlite(self.path) as conn:
            conn.executemany('INSERT INTO event VALUES(:id,:cmd,:room_id,:title,:timestamp)', events)
        actual = list_live_notice_events_after(10, 15, self.now-db.MAX_NOTICE_AGE, self.path)
        self.assertEqual([r['id'] for r in actual], [11, 12, 13])

    def test_retry_is_independent_per_group_and_preserves_order(self):
        self.queue(self.row(11, 'LIVE'), self.row(12))
        db.retry_live_notice(11, 1, self.path, now=self.now)
        db.mark_live_notice_sent(11, 2, db_path=self.path)
        ready = db.list_pending_live_notices(self.path, now=self.now)
        self.assertEqual([(r['event_id'],r['group_id']) for r in ready], [(12, 2)])
        ready = db.list_pending_live_notices(self.path, now=self.now+5)
        self.assertEqual([(r['event_id'],r['group_id']) for r in ready], [(11, 1), (12, 2)])

    def test_backoff_is_bounded(self):
        self.queue(self.row())
        for _ in range(15):
            db.retry_live_notice(11, 1, self.path, now=self.now)
        self.assertEqual(self.records()[0]['retry_after'], self.now+300)
        self.assertEqual(self.records()[0]['attempts'], 15)

    def test_recovery_window_includes_ten_minutes_but_not_old_notices(self):
        self.queue(self.row(age=600), self.row(12, age=db.MAX_NOTICE_AGE+1))
        self.assertEqual({r['event_id'] for r in db.list_pending_live_notices(self.path)}, {11})

    def test_unsubscribed_group_is_not_sent_and_does_not_block_a_new_room(self):
        self.queue(self.row())
        with connect_sqlite(self.path) as conn:
            conn.execute('UPDATE subscription SET room_id=200 WHERE group_id=1')
        self.queue(self.row(12, room=200))
        ready = db.list_pending_live_notices(self.path)
        self.assertEqual([(r['event_id'],r['group_id']) for r in ready], [(11, 2), (12, 1)])

    def test_cleanup_only_touches_notification_records(self):
        self.queue(self.row(age=8*86400))
        db.prune_live_notices(self.path)
        self.assertEqual(self.records(), [])
        self.assertEqual(self.cursor(), '11')
        with connect_sqlite(self.path) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM subscription').fetchone()[0], 2)


class LiveNoticeWatcherTests(Fixtures, unittest.IsolatedAsyncioTestCase):
    def environment(self, bots, events=()):
        def state(key):
            self.assertEqual(key, 'last_event_id')
            return self.cursor()

        env = {
            'asyncio': asyncio, 'time': time, 'Any': Any,
            'logger': logging.getLogger('test.live_notice'),
            'get_bots': lambda: bots, '_format_name': AsyncMock(return_value='三理'),
            '_available_notification_groups': AsyncMock(return_value=None),
            'get_state': state, 'get_max_event_id': lambda: max([10]+[r['id'] for r in events]),
            'list_live_notice_events_after': lambda last, through, since: [
                r for r in events if last<r['id']<=through and r['timestamp']>=since],
            'is_streaming_event': lambda row: False, 'is_duplicate_room_change': lambda row: False,
            'stage_live_notices': partial(db.stage_live_notices, db_path=self.path),
            'list_pending_live_notices': partial(db.list_pending_live_notices, self.path),
            'mark_live_notice_sent': partial(db.mark_live_notice_sent, db_path=self.path),
            'retry_live_notice': partial(db.retry_live_notice, db_path=self.path),
            'prune_live_notices': partial(db.prune_live_notices, self.path),
            'MAX_NOTICE_AGE': db.MAX_NOTICE_AGE, '_is_mention_all_enabled': lambda group: group==2,
            'Message': list, 'MessageSegment': SimpleNamespace(at=lambda target: ('at', target),
                                                               text=lambda text: ('text', text)),
            '_live_event_cursor': None, '_live_event_cursor_saved_at': 0., '_live_notice_pruned_at': 0.,
        }
        tree = ast.parse(Path('src/plugins/scheduler.py').read_text())
        nodes = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                 and n.name in {'_build_message', '_deliver_live_notices', 'watch_live_events'}]
        for node in nodes:
            node.decorator_list = []
        exec(compile(ast.Module(body=nodes, type_ignores=[]), '<live-watcher>', 'exec'), env)
        return env

    async def test_offline_notice_is_recovered_after_restart_even_without_new_events(self):
        bots = {}
        events = [self.row()]
        env = self.environment(bots, events)
        await env['watch_live_events']()
        self.assertEqual(self.cursor(), '11')
        self.assertTrue(all(r['status']=='pending' and r['attempts']==0 for r in self.records()))
        bot = SimpleNamespace(call_api=AsyncMock(return_value={'message_id': 123}))
        bots[1] = bot
        restarted = self.environment(bots, events)
        await restarted['watch_live_events']()
        self.assertEqual(bot.call_api.await_count, 2)
        self.assertTrue(all(r['status']=='sent' for r in self.records()))
        await restarted['watch_live_events']()
        self.assertEqual(bot.call_api.await_count, 2)

    async def test_one_failed_group_does_not_block_others_or_resend_successful_groups(self):
        self.queue(self.row())
        bot = SimpleNamespace(call_api=AsyncMock(side_effect=[RuntimeError('offline'), {'message_id': 20}]))
        env = self.environment({1: bot})
        await env['_deliver_live_notices']()
        self.assertEqual([r['status'] for r in self.records()], ['pending', 'sent'])
        bot.call_api.side_effect = None
        bot.call_api.return_value = {'message_id': 21}
        bot.call_api.reset_mock()
        with patch('time.time', return_value=self.now+6):
            await env['_deliver_live_notices']()
        bot.call_api.assert_awaited_once_with('send_group_msg', group_id=1, message='三理下播了...')
        self.assertTrue(all(r['status']=='sent' for r in self.records()))

    async def test_cancellation_during_send_keeps_delivery_pending(self):
        self.queue(self.row())
        bot = SimpleNamespace(call_api=AsyncMock(side_effect=asyncio.CancelledError()))
        env = self.environment({1: bot})
        with self.assertRaises(asyncio.CancelledError):
            await env['_deliver_live_notices']()
        self.assertTrue(all(r['status']=='pending' for r in self.records()))

    async def test_unjoined_group_is_not_sent_or_retried_and_remains_recoverable(self):
        self.queue(self.row())
        bot = SimpleNamespace(call_api=AsyncMock(return_value={'message_id': 10}))
        env = self.environment({1: bot})
        env['_available_notification_groups'].return_value = {2}
        await env['_deliver_live_notices']()
        self.assertEqual([(r['group_id'], r['status'], r['attempts']) for r in self.records()],
                         [(1, 'pending', 0), (2, 'sent', 0)])
        self.assertEqual(bot.call_api.await_count, 1)
        env['_available_notification_groups'].return_value = {1, 2}
        await env['_deliver_live_notices']()
        self.assertTrue(all(r['status']=='sent' for r in self.records()))

    async def test_multiple_offline_events_are_delivered_in_order_with_mention_all_only_on_live(self):
        bots = {}
        events = [self.row(11, 'LIVE'), self.row(12, 'ROOM_CHANGE'), self.row(13)]
        env = self.environment(bots, events)
        await env['watch_live_events']()
        self.assertEqual(len(self.records()), 6)
        bot = SimpleNamespace(call_api=AsyncMock(return_value={'message_id': 1}))
        bots[1] = bot
        for _ in events:
            await env['watch_live_events']()
        calls = bot.call_api.await_args_list
        self.assertEqual([c.kwargs['group_id'] for c in calls], [1,2,1,2,1,2])
        self.assertEqual(calls[0].kwargs['message'], '三理开播了！')
        self.assertEqual(calls[1].kwargs['message'], [('at','all'), ('text','三理开播了！')])
        self.assertEqual(calls[4].kwargs['message'], '三理下播了...')
        self.assertTrue(all(r['status']=='sent' for r in self.records()))
