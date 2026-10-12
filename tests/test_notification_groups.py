import ast
import asyncio
import logging
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


class NotificationGroupsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        env = {'asyncio': asyncio, 'time': time, '_notification_group_cache': {},
               'logger': logging.getLogger('test.notification_groups')}
        tree = ast.parse(Path('src/plugins/scheduler.py').read_text())
        nodes = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                 and n.name == '_available_notification_groups']
        exec(compile(ast.Module(body=nodes, type_ignores=[]), '<notification-groups>', 'exec'), env)
        self.get_groups = env['_available_notification_groups']

    async def test_success_is_cached_for_five_minutes(self):
        bot = SimpleNamespace(self_id='1', call_api=AsyncMock(return_value=[{'group_id': 100}]))
        with patch('time.monotonic', return_value=1000):
            self.assertEqual(await self.get_groups(bot), {100})
            self.assertEqual(await self.get_groups(bot), {100})
        bot.call_api.assert_awaited_once_with('get_group_list')
        with patch('time.monotonic', return_value=1301):
            self.assertEqual(await self.get_groups(bot), {100})
        self.assertEqual(bot.call_api.await_count, 2)

    async def test_api_failure_preserves_sending_route_and_has_short_cache(self):
        bot = SimpleNamespace(self_id='1', call_api=AsyncMock(side_effect=RuntimeError('offline')))
        with patch('time.monotonic', return_value=1000):
            self.assertIsNone(await self.get_groups(bot))
            self.assertIsNone(await self.get_groups(bot))
        self.assertEqual(bot.call_api.await_count, 1)
        bot.call_api.side_effect = None
        bot.call_api.return_value = [{'group_id': 100}]
        with patch('time.monotonic', return_value=1031):
            self.assertEqual(await self.get_groups(bot), {100})

    async def test_empty_list_is_confirmed_but_rechecked_soon(self):
        bot = SimpleNamespace(self_id='1', call_api=AsyncMock(return_value=[]))
        with patch('time.monotonic', return_value=1000):
            self.assertEqual(await self.get_groups(bot), set())
        bot.call_api.return_value = [{'group_id': 100}]
        with patch('time.monotonic', return_value=1031):
            self.assertEqual(await self.get_groups(bot), {100})

    async def test_cancellation_is_propagated(self):
        bot = SimpleNamespace(self_id='1', call_api=AsyncMock(side_effect=asyncio.CancelledError()))
        with self.assertRaises(asyncio.CancelledError):
            await self.get_groups(bot)
