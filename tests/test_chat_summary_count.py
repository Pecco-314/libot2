import ast
import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.common import chat_summary_service as service
from src.db import chat_summary as db
from tests.command_sources import command_tree
from tests.test_chat_summary_batches import Fixtures, rows, result as summary_result


class CountTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / 'context.db'
        db.init_db(self.path)
        with db.connection(self.path) as conn:
            for bid in range(1, 5):
                result = {
                    'topics': [{'title': f'话题{bid}', 'summary': '讨论内容',
                                'message_ids': [str(bid * 500)], 'participants': []}],
                    'message_count': 400, 'participants_version': 1,
                }
                conn.execute(
                    "INSERT INTO chat_summary_batch(id,group_id,start_ts,end_ts,message_count,data_hash,status,result,created_at) "
                    "VALUES(?,1,?,?,500,?,'complete',?,0)",
                    (bid, bid * 500, bid * 500 + 499, str(bid), json.dumps(result)),
                )
                conn.executemany('INSERT INTO chat_summary_member VALUES(?,1,?,?)',
                                 [(bid, mid, mid) for mid in range(bid * 500, bid * 500 + 500)])

    def tearDown(self):
        self.folder.cleanup()

    def test_count_arguments(self):
        for argument, expected in [('', 500), ('500', 500), ('1200条', 1200), ('5000', 5000)]:
            self.assertEqual(service.parse_summary_count(argument), expected)
        for bad in ('1小时', '499', '5001', '-500', '500.0', '500 1000'):
            with self.assertRaises(ValueError):
                service.parse_summary_count(bad)

    def test_query_arguments_distinguish_counts_and_time(self):
        now = datetime(2026, 10, 13, 16, 30, tzinfo=service.TZ)
        for text, expected in (('', 500), ('2000', 2000), ('5000条', 5000)):
            self.assertEqual(service.parse_summary_query(text, now=now), expected)
        for text in ('30分钟', '2小时', '08:00', '2026-10-13 08:00', '08:00 10:00'):
            with self.subTest(text=text):
                self.assertEqual(service.parse_summary_query(text, now=now),
                                 service.parse_period(text, now))
        for text in ('5001', '25小时', '错误', '2026-10-13 18:00'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                service.parse_summary_query(text, now=now)

    async def test_cached_ranges_round_up_without_model_or_cursor_changes(self):
        before = db.get_cycle(1, self.path)
        with patch.object(service, '_complete', AsyncMock()) as model, patch.object(
            service, 'summarize', AsyncMock()
        ) as summarize:
            result = await service.cached_count_summary(1, 1200, db_path=self.path)
            self.assertEqual(result['batch_ids'], [2, 3, 4])
            self.assertEqual(result['raw_message_count'], 1500)
            self.assertEqual(result['message_count'], 1200)
            self.assertEqual(len(result['topics']), 3)
            self.assertIn('batch_period', result['topics'][0])
            model.assert_not_called()
            summarize.assert_not_called()
        self.assertEqual(db.get_cycle(1, self.path), before)
        result = await service.cached_count_summary(1, 5000, db_path=self.path)
        self.assertEqual(result['raw_message_count'], 2000)

    def test_overlapping_cached_batch_is_not_double_counted(self):
        with db.connection(self.path) as conn:
            conn.execute(
                "INSERT INTO chat_summary_batch(id,group_id,start_ts,end_ts,message_count,data_hash,status,result,created_at) "
                "SELECT 5,group_id,start_ts,end_ts,message_count,'duplicate',status,result,created_at "
                "FROM chat_summary_batch WHERE id=4"
            )
            conn.execute('INSERT INTO chat_summary_member SELECT 5,group_id,message_id,sent_at '
                         'FROM chat_summary_member WHERE batch_id=4')
        batches = db.recent_count_batches(1, 1200, self.path)
        self.assertEqual([b['id'] for b in batches], [2, 3, 5])

    async def test_combined_cached_batches_keep_all_topics(self):
        with db.connection(self.path) as conn:
            for bid in (3, 4):
                value = {
                    'topics': [
                        {'title': f'批次{bid}话题{i}', 'summary': '讨论内容',
                         'message_ids': [str(bid * 500 + i)], 'participants': []}
                        for i in range(12)
                    ],
                    'message_count': 400, 'participants_version': 1,
                }
                conn.execute('UPDATE chat_summary_batch SET result=? WHERE id=?',
                             (json.dumps(value), bid))
        with patch.object(service, '_complete', AsyncMock()) as model:
            result = await service.cached_count_summary(1, 1000, db_path=self.path)
        self.assertEqual(result['batch_ids'], [3, 4])
        self.assertEqual(len(result['topics']), 24)
        model.assert_not_called()

    def test_small_batches_are_not_limited_to_latest_100_records(self):
        with db.connection(self.path) as conn:
            conn.execute("DELETE FROM chat_summary_member")
            conn.execute("DELETE FROM chat_summary_batch")
            for bid in range(1, 122):
                value = {'topics': [], 'terms_to_check': [], 'message_count': 1}
                conn.execute(
                    "INSERT INTO chat_summary_batch(id,group_id,start_ts,end_ts,message_count,data_hash,status,result,created_at,origin) "
                    "VALUES(?,1,?,?,1,?,'complete',?,0,'query')",
                    (bid, bid, bid, str(bid), json.dumps(value)),
                )
                conn.execute('INSERT INTO chat_summary_member VALUES(?,1,?,?)', (bid, bid, bid))
        batches = db.recent_count_batches(1, 500, self.path)
        self.assertEqual(len(batches), 121)
        self.assertEqual(sum(b['message_count'] for b in batches), 121)


class TimeQueryTests(Fixtures, unittest.IsolatedAsyncioTestCase):
    async def test_over_limit_rejected_before_generation_or_cache_changes(self):
        records = rows(5001)
        self.insert(records)
        before = db.get_cycle(1, self.path)
        with patch.object(service, 'summarize', AsyncMock()) as model, patch.object(
            service, '_complete', AsyncMock()
        ) as merge, patch.object(db, 'init_db') as init:
            with self.assertRaisesRegex(ValueError, '5001条.*超过5000'):
                await service.query_summary(1, records[0]['sent_at'], records[-1]['sent_at'] + 1,
                                            '测试', db_path=self.path, cache_path=self.cache)
        model.assert_not_called()
        merge.assert_not_called()
        init.assert_not_called()
        self.assertEqual(db.get_cycle(1, self.path), before)

    async def test_exact_limit_excludes_bot_and_summary_requests(self):
        records = rows(5002)
        records[-2]['user_id'] = 99999
        records[-1]['segments'] = json.dumps([{'type': 'text', 'text': '在聊什么 2小时'}])
        self.insert(records)
        before = db.get_cycle(1, self.path)
        notice = AsyncMock()
        fake = AsyncMock(side_effect=lambda msgs, *a, **kw: summary_result(msgs[0]['id']))
        with patch.object(service, 'summarize', fake), patch.object(
            service, '_complete', AsyncMock(return_value=(summary_result(), {}))
        ):
            value = await service.query_summary(
                1, records[0]['sent_at'], records[-1]['sent_at'] + 1, '测试',
                db_path=self.path, cache_path=self.cache, bot_qq='99999', on_generate=notice,
            )
        self.assertEqual(value['raw_message_count'], 5000)
        self.assertEqual(fake.await_count, 10)
        notice.assert_awaited_once()
        self.assertTrue(value['topics'][0]['participants'])
        self.assertEqual(db.get_cycle(1, self.path), before)
        with patch.object(service, 'summarize', AsyncMock()) as model, patch.object(
            service, '_complete', AsyncMock()
        ) as merge:
            cached = await service.query_summary(
                1, records[0]['sent_at'], records[-1]['sent_at'] + 1, '测试',
                db_path=self.path, cache_path=self.cache, bot_qq='99999',
            )
        self.assertTrue(cached['cache_hit'])
        model.assert_not_called()
        merge.assert_not_called()

    async def test_time_boundaries_are_strict_and_show_participants(self):
        records = rows(100)
        self.insert(records)
        fake = AsyncMock(side_effect=lambda msgs, *a, **kw: summary_result(msgs[0]['id']))
        with patch.object(service, 'summarize', fake):
            value = await service.query_summary(
                1, records[10]['sent_at'], records[30]['sent_at'], '测试',
                db_path=self.path, cache_path=self.cache,
            )
        self.assertEqual(value['raw_message_count'], 20)
        self.assertEqual([m['id'] for m in fake.call_args.args[0]],
                         [str(r['message_id']) for r in records[10:30]])
        self.assertEqual(value['topics'][0]['participants'][0]['name'], '甲')


class ClosedJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_handler_notifies_on_generation_and_always_uses_forward(self):
        tree = command_tree()
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == 'handle_chat_summary')
        node.decorator_list = []
        cases = ((False, False), (True, False), (True, True))
        for argument, generates, send_fails in (
            (argument, *case) for argument in ('', '30分钟') for case in cases
        ):
            with self.subTest(argument=argument, generates=generates, send_fails=send_fails):
                order = []

                async def notice(text):
                    self.assertEqual(text, '正在总结中，请稍等……')
                    order.append('notice')
                    if send_fails:
                        raise RuntimeError('消息暂时发不出')

                async def summary(group, *selection, on_generate, **kwargs):
                    self.assertEqual(len(selection), 3 if argument else 1)
                    if generates:
                        await on_generate()
                    order.append('model' if generates else 'cache')
                    return {}

                forward = AsyncMock(side_effect=lambda *a: order.append('forward'))
                env = {
                    'asyncio': asyncio, 'Matcher': object, 'Bot': object, 'Event': object,
                    'CommandArg': lambda: None, 'get_group_id': lambda e: 1,
                    'get_command_config': lambda *a: SimpleNamespace(enabled=True),
                    'parse_summary_query': service.parse_summary_query,
                    '_CHAT_SUMMARY_BUSY': set(), '_CHAT_SUMMARY_SLOTS': asyncio.Semaphore(2),
                    'count_summary': summary, 'query_summary': summary,
                    'render_summary_images': AsyncMock(return_value=['/tmp/one.png', '/tmp/two.png']),
                    'MessageSegment': SimpleNamespace(image=lambda **kw: kw),
                    'send_forward_message': forward,
                    'logger': SimpleNamespace(warning=lambda *a: None),
                }
                exec(compile(ast.Module(body=[node], type_ignores=[]), '<handler>', 'exec'), env)
                matcher = SimpleNamespace(send=AsyncMock(side_effect=notice), finish=AsyncMock())
                await env['handle_chat_summary'](
                    matcher, SimpleNamespace(self_id='99999'), object(),
                    SimpleNamespace(extract_plain_text=lambda: argument),
                )
                self.assertEqual(order, ['notice', 'model', 'forward'] if generates else ['cache', 'forward'])
                self.assertEqual(len(forward.call_args.args[2]), 2)
                self.assertFalse(env['_CHAT_SUMMARY_BUSY'])

    async def test_disabled_group_is_not_staged_or_processed(self):
        tree = ast.parse(Path('src/spider/jobs/chat_summary.py').read_text())
        functions = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)]
        def stage(group):
            self.fail('disabled group staged')
        process = AsyncMock()
        env = {
            'asyncio': asyncio, 'group_ids': lambda: [], 'stage_ready': stage,
            'process_batch': process,
            'db': SimpleNamespace(init_db=lambda: None, ready_batch_ids=lambda: [1],
                                  get_batch=lambda bid: {'group_id': 1}),
        }
        exec(compile(ast.Module(body=functions, type_ignores=[]), '<jobs>', 'exec'), env)
        await env['stage_chat_summaries']()
        await env['collect_chat_summaries']()
        process.assert_not_called()

    async def test_disabled_handler_does_not_bypass_for_manager(self):
        tree = command_tree()
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == 'handle_chat_summary')
        node.decorator_list = []

        class Finished(Exception):
            pass

        finish = AsyncMock(side_effect=Finished)
        summarize = AsyncMock()
        env = {
            'asyncio': asyncio, 'Matcher': object, 'Bot': object, 'Event': object,
            'CommandArg': lambda: None, 'get_group_id': lambda e: 1,
            'get_command_config': lambda *a: SimpleNamespace(enabled=False),
            'count_summary': summarize,
        }
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<handler>', 'exec'), env)
        with self.assertRaises(Finished):
            await env['handle_chat_summary'](SimpleNamespace(finish=finish), None, None)
        summarize.assert_not_called()
        self.assertIn('打开功能 在聊什么', finish.call_args.args[0])
