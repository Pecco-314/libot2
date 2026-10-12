import ast
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.common import chat_summary_service as service
from src.db import chat_summary as db


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


class ClosedJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_handler_notifies_on_generation_and_always_uses_forward(self):
        tree = ast.parse(Path('src/plugins/commands.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == 'handle_chat_summary')
        node.decorator_list = []
        for generates, send_fails in ((False, False), (True, False), (True, True)):
            with self.subTest(generates=generates, send_fails=send_fails):
                order = []

                async def notice(text):
                    self.assertEqual(text, '正在总结中，请稍等……')
                    order.append('notice')
                    if send_fails:
                        raise RuntimeError('消息暂时发不出')

                async def summary(group, count, *, on_generate, **kwargs):
                    if generates:
                        await on_generate()
                    order.append('model' if generates else 'cache')
                    return {}

                forward = AsyncMock(side_effect=lambda *a: order.append('forward'))
                env = {
                    'asyncio': asyncio, 'Matcher': object, 'Bot': object, 'Event': object,
                    'CommandArg': lambda: None, 'get_group_id': lambda e: 1,
                    'get_command_config': lambda *a: SimpleNamespace(enabled=True),
                    'parse_summary_count': service.parse_summary_count,
                    '_CHAT_SUMMARY_BUSY': set(), '_CHAT_SUMMARY_SLOTS': asyncio.Semaphore(2),
                    'count_summary': summary,
                    'render_summary_images': AsyncMock(return_value=['/tmp/one.png', '/tmp/two.png']),
                    'MessageSegment': SimpleNamespace(image=lambda **kw: kw),
                    'send_forward_message': forward,
                    'logger': SimpleNamespace(warning=lambda *a: None),
                }
                exec(compile(ast.Module(body=[node], type_ignores=[]), '<handler>', 'exec'), env)
                matcher = SimpleNamespace(send=AsyncMock(side_effect=notice), finish=AsyncMock())
                await env['handle_chat_summary'](
                    matcher, SimpleNamespace(self_id='99999'), object(),
                    SimpleNamespace(extract_plain_text=lambda: ''),
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
        tree = ast.parse(Path('src/plugins/commands.py').read_text())
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
