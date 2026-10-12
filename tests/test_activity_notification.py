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
from unittest.mock import AsyncMock, patch

from src.db import activity as activity_db
from src.db import activity_notice as db
from src.db.sqlite import connect_sqlite


class Fixtures:
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'test.db'
        self.now = int(time.time())
        activity_db.init_activity_db(self.path)
        db.init_activity_notice_db(self.path)
        with connect_sqlite(self.path) as conn:
            conn.executescript("""
                CREATE TABLE state(key TEXT PRIMARY KEY,value TEXT,updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
                INSERT INTO state(key,value) VALUES('last_activity_id','0');
                CREATE TABLE subscription(group_id INTEGER PRIMARY KEY,room_id INTEGER);
                INSERT INTO subscription VALUES(1,100),(2,100);
            """)

    def tearDown(self):
        self.directory.cleanup()

    def insert(self, *, age=0, dy_type='DYNAMIC_TYPE_DRAW', name='三理Mit3uri'):
        activity_db.insert_activity(
            activity_id='1234567890123456789', room_id=100, uid=10, uname=name,
            timestamp=self.now-age, dy_type_str=dy_type, item_dict={}, db_path=self.path,
        )
        return self.activities_after(0)[0]

    def activities_after(self, last):
        with connect_sqlite(self.path) as conn:
            rows = conn.execute(
                f'SELECT {activity_db.ACTIVITY_SELECT} FROM activity WHERE id>? ORDER BY id', (last,),
            ).fetchall()
        return [activity_db._row_to_dict(row) for row in rows]

    def records(self):
        with connect_sqlite(self.path) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(
                'SELECT * FROM activity_notice_delivery ORDER BY activity_record_id,group_id',
            )]

    def cursor(self):
        with connect_sqlite(self.path) as conn:
            return int(conn.execute("SELECT value FROM state WHERE key='last_activity_id'").fetchone()[0])

    def environment(self, bots):
        self.render = AsyncMock(return_value=Path('/tmp/activity.png'))
        env = dict(
            asyncio=asyncio, time=time, logger=logging.getLogger('test.activity_notice'),
            get_bots=lambda: bots, _ensure_last_activity_id_initialized=AsyncMock(),
            _available_notification_groups=AsyncMock(return_value=None),
            _get_last_activity_id=self.cursor, list_activities_after=self.activities_after,
            _render_activity_image=self.render,
            stage_activity_notices=partial(db.stage_activity_notices, db_path=self.path),
            list_pending_activity_notices=partial(db.list_pending_activity_notices, self.path),
            mark_activity_notice_sent=partial(db.mark_activity_notice_sent, db_path=self.path),
            retry_activity_notice=partial(db.retry_activity_notice, db_path=self.path),
            prune_activity_notices=partial(db.prune_activity_notices, self.path),
            MAX_ACTIVITY_NOTICE_AGE=db.MAX_ACTIVITY_NOTICE_AGE, _activity_notice_pruned_at=0.,
            Message=list, MessageSegment=SimpleNamespace(
                text=lambda text: ('text', text), image=lambda **kw: ('image', kw['file']),
            ), LIVE_RCMD_ACTIVITY_TYPE=activity_db.LIVE_RCMD_ACTIVITY_TYPE,
        )
        tree = ast.parse(Path('src/plugins/scheduler.py').read_text())
        nodes = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                 and n.name in {'watch_activities', '_deliver_activity_notices'}]
        for node in nodes:
            node.decorator_list = []
        exec(compile(ast.Module(body=nodes, type_ignores=[]), '<activity-watcher>', 'exec'), env)
        return env


class ActivityNotificationTest(Fixtures, unittest.IsolatedAsyncioTestCase):
    def bot(self, **kwargs):
        return SimpleNamespace(call_api=AsyncMock(return_value={'message_id': 123}, **kwargs))

    async def test_missed_activity_older_than_ten_minutes_is_sent_with_link_after_image(self):
        self.insert(age=2400)
        bot = self.bot()
        env = self.environment({1: bot})
        await env['watch_activities']()
        bot.call_api.assert_any_await('send_group_msg', group_id=1, message=[
            ('text', '三理Mit3uri发布了新动态！\n'), ('image', '/tmp/activity.png'),
            ('text', '\nhttps://t.bilibili.com/1234567890123456789'),
        ])
        self.assertEqual(self.cursor(), 1)
        self.assertTrue(all(row['status']=='sent' for row in self.records()))

    async def test_offline_restart_sends_pending_even_without_new_rows(self):
        self.insert()
        env = self.environment({})
        await env['watch_activities']()
        self.assertEqual(self.cursor(), 1)
        self.assertTrue(all(row['attempts']==0 for row in self.records()))
        bot = self.bot()
        restarted = self.environment({1: bot})
        await restarted['watch_activities']()
        self.assertEqual(bot.call_api.await_count, 2)
        await restarted['watch_activities']()
        self.assertEqual(bot.call_api.await_count, 2)

    async def test_failure_in_one_group_does_not_repeat_successful_group(self):
        self.insert()
        bot = self.bot(side_effect=[RuntimeError('removed from group'), {'message_id': 456}])
        env = self.environment({1: bot})
        await env['watch_activities']()
        self.assertEqual([row['status'] for row in self.records()], ['pending', 'sent'])
        bot.call_api.reset_mock()
        bot.call_api.side_effect = None
        with patch('time.time', return_value=self.now+6):
            await env['watch_activities']()
        self.assertEqual(bot.call_api.await_count, 1)
        self.assertEqual(bot.call_api.await_args.kwargs['group_id'], 1)

    async def test_render_failure_keeps_pending(self):
        self.insert()
        bot = self.bot()
        env = self.environment({1: bot})
        self.render.return_value = None
        await env['watch_activities']()
        bot.call_api.assert_not_awaited()
        self.assertTrue(all(row['status']=='pending' for row in self.records()))
        self.render.return_value = Path('/tmp/activity.png')
        with patch('time.time', return_value=self.now+6):
            await env['watch_activities']()
        self.assertEqual(bot.call_api.await_count, 2)

    async def test_unjoined_group_does_not_upload_and_is_kept_for_rejoining(self):
        self.insert()
        bot = self.bot()
        env = self.environment({1: bot})
        env['_available_notification_groups'].return_value = {2}
        await env['watch_activities']()
        self.assertEqual([(r['group_id'], r['status'], r['attempts']) for r in self.records()],
                         [(1, 'pending', 0), (2, 'sent', 0)])
        self.assertEqual(self.render.await_count, 1)
        env['_available_notification_groups'].return_value = {1, 2}
        await env['watch_activities']()
        self.assertTrue(all(r['status']=='sent' for r in self.records()))

    async def test_live_recommendation_and_older_than_one_day_stay_silent(self):
        self.insert(dy_type=activity_db.LIVE_RCMD_ACTIVITY_TYPE)
        bot = self.bot()
        env = self.environment({1: bot})
        await env['watch_activities']()
        bot.call_api.assert_not_awaited()
        self.assertEqual(self.cursor(), 1)
        self.assertEqual(self.records(), [])
        with connect_sqlite(self.path) as conn:
            conn.execute('UPDATE activity SET id=2,dy_type_str=?,timestamp=?',
                         ('DYNAMIC_TYPE_DRAW', self.now-db.MAX_ACTIVITY_NOTICE_AGE-1))
        await env['watch_activities']()
        bot.call_api.assert_not_awaited()
        self.assertEqual(self.cursor(), 2)

    async def test_cancellation_leaves_notice_pending(self):
        self.insert()
        env = self.environment({1: self.bot(side_effect=asyncio.CancelledError())})
        with self.assertRaises(asyncio.CancelledError):
            await env['watch_activities']()
        self.assertTrue(all(row['status']=='pending' for row in self.records()))

    async def test_fallback_name(self):
        self.insert(name='')
        bot = self.bot()
        env = self.environment({1: bot})
        await env['watch_activities']()
        self.assertTrue(bot.call_api.await_args.kwargs['message'][0][1].startswith('UP主发布了新动态！'))


class ActivityNoticeDBTests(Fixtures, unittest.TestCase):
    def test_enqueue_and_cursor_are_atomic(self):
        row = self.insert()
        with connect_sqlite(self.path) as conn:
            conn.execute("""CREATE TRIGGER fail_insert BEFORE INSERT ON activity_notice_delivery
                         WHEN NEW.group_id=2 BEGIN SELECT RAISE(ABORT,'test failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            db.stage_activity_notices([row], row['id'], self.path)
        self.assertEqual(self.cursor(), 0)
        self.assertEqual(self.records(), [])

    def test_repeated_discovery_does_not_reset_sent_status(self):
        row = self.insert()
        db.stage_activity_notices([row], row['id'], self.path)
        db.mark_activity_notice_sent(row['id'], 1, 123, self.path)
        db.stage_activity_notices([row], row['id'], self.path)
        self.assertEqual(len(self.records()), 2)
        self.assertEqual(self.records()[0]['status'], 'sent')
        self.assertEqual(self.records()[0]['message_id'], 123)

    def test_unsubscribed_group_not_sent(self):
        row = self.insert()
        db.stage_activity_notices([row], row['id'], self.path)
        with connect_sqlite(self.path) as conn:
            conn.execute('DELETE FROM subscription WHERE group_id=1')
        self.assertEqual([r['group_id'] for r in db.list_pending_activity_notices(self.path)], [2])

    def test_initialization_checks_only_activity_related_foreign_keys(self):
        with connect_sqlite(self.path) as conn:
            conn.execute('PRAGMA foreign_keys=OFF')
            conn.executescript("""
                CREATE TABLE other_parent(id INTEGER PRIMARY KEY);
                CREATE TABLE unrelated(parent_id INTEGER REFERENCES other_parent(id));
                INSERT INTO unrelated VALUES(999);
            """)
        activity_db.init_activity_db(self.path)
        with connect_sqlite(self.path) as conn:
            conn.execute('PRAGMA foreign_keys=OFF')
            conn.execute("INSERT INTO activity_asset(activity_id,remote_url,local_path,content_sha256,size_bytes) "
                         "VALUES('missing','url','path','hash',0)")
        with self.assertRaisesRegex(RuntimeError, 'foreign key violations'):
            activity_db.init_activity_db(self.path)
