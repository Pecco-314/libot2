import asyncio
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.monitor.monitor import (
    _build_session, _recover_api_live_start, _recover_stopped_clients, _writer_loop,
)


class MonitorHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_stopped_client_is_replaced(self):
        stopped = SimpleNamespace(is_running=False, close=AsyncMock())
        healthy = SimpleNamespace(is_running=True, close=AsyncMock())
        clients = {100: stopped, 200: healthy}
        start = AsyncMock()
        await _recover_stopped_clients(clients, start)
        stopped.close.assert_awaited_once()
        healthy.close.assert_not_awaited()
        start.assert_awaited_once_with(100)
        self.assertEqual(clients, {200: healthy})

    async def test_concurrent_replacement_is_preserved(self):
        replacement = SimpleNamespace(is_running=True)
        clients = {}

        async def close():
            clients[100] = replacement

        clients[100] = SimpleNamespace(is_running=False, close=close)
        start = AsyncMock()
        await _recover_stopped_clients(clients, start)
        self.assertIs(clients[100], replacement)
        start.assert_not_awaited()

    async def test_api_recovers_actual_beijing_start_time(self):
        response = Mock()
        response.json = AsyncMock(return_value={
            'code': 0, 'data': {'live_status': 1, 'live_time': '2026-10-11 19:30:27',
                               'title': '鸣潮3.7！'},
        })
        context = AsyncMock()
        context.__aenter__.return_value = response
        session = Mock()
        session.get.return_value = context
        with patch('src.monitor.monitor.recover_live_start', return_value=1) as recover:
            await _recover_api_live_start(session, 100, Path('example.db'))
        recover.assert_called_once_with(100, 1791718227, title='鸣潮3.7！',
                                        db_path=Path('example.db'))

    async def test_offline_or_failed_api_does_not_create_marker(self):
        for payload in ({'code': 0, 'data': {'live_status': 0}}, {'code': -1}):
            with self.subTest(payload=payload):
                response = Mock(json=AsyncMock(return_value=payload))
                context = AsyncMock()
                context.__aenter__.return_value = response
                session = Mock(get=Mock(return_value=context))
                with patch('src.monitor.monitor.recover_live_start') as recover:
                    await _recover_api_live_start(session, 100, Path('example.db'))
                recover.assert_not_called()

    async def test_database_write_leaves_event_loop_available(self):
        loop = asyncio.get_running_loop()
        entered = asyncio.Event()
        release = threading.Event()
        stop = asyncio.Event()
        queue = asyncio.Queue()
        row = (100, 'DANMU_MSG', 1, '名字', '内容', None, None, None, None, 1)
        queue.put_nowait(row)
        thread_ids = []

        def insert(rows):
            thread_ids.append(threading.get_ident())
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(2):
                raise RuntimeError('event loop was blocked')
            self.assertEqual(rows, [row])

        task = asyncio.create_task(_writer_loop(SimpleNamespace(insert_many=insert), queue, stop,
                                                batch_size=1, flush_interval=.01))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertNotEqual(thread_ids[0], threading.get_ident())
        finally:
            stop.set()
            release.set()
            await asyncio.wait_for(task, 3)

    async def test_transport_explicitly_ignores_environment_proxy(self):
        session = _build_session({})
        try:
            self.assertFalse(session.trust_env)
        finally:
            await session.close()
