import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from src.render import dc


class DcRequestsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        lock_patch = patch.object(dc, '_DC_REQUEST_LOCK', asyncio.Lock())
        lock_patch.start()
        self.addCleanup(lock_patch.stop)

    async def test_queries_and_sources_share_single_request_slot(self):
        active = peak = 0
        calls = []

        async def get(_client, url, *, params, timeout):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            calls.append((url, params['month']))
            try:
                await asyncio.sleep(.005)
                return httpx.Response(200, request=httpx.Request('GET', url), json=[{
                    'room_id': 1, 'month': params['month'], 'anchor_name': '主播',
                    'gift': 10, 'guard': 20, 'super_chat': 30,
                }])
            finally:
                active -= 1

        with patch.object(httpx.AsyncClient, 'get', get):
            first, second = await asyncio.gather(
                dc.get_dc_data('all', '2026-01', '2026-03'),
                dc.get_dc_data('vr', '2026-02'),
            )
        self.assertEqual(peak, 1)
        self.assertEqual(len(calls), 7)
        self.assertEqual(first[0]['total_revenue'], 180)
        self.assertEqual(second[0]['total_revenue'], 60)

    async def test_failed_month_aborts_without_partial_total_or_later_requests(self):
        failure = httpx.HTTPStatusError(
            'busy', request=httpx.Request('GET', 'https://example.test'),
            response=httpx.Response(503),
        )
        fetch = AsyncMock(side_effect=[[{'room_id': 1}], failure])
        with patch.object(dc, 'fetch_dc_data', fetch):
            with self.assertRaises(httpx.HTTPStatusError):
                await dc.get_dc_data('vr', '2026-01', '2026-03')
        self.assertEqual([call.args[1] for call in fetch.await_args_list], ['202601', '202602'])

    async def test_empty_month_is_not_silently_skipped(self):
        with patch.object(dc, 'fetch_dc_data', AsyncMock(return_value=[])):
            with self.assertRaisesRegex(RuntimeError, '202601'):
                await dc.get_dc_data('vr', '2026-01', '2026-03')

    async def test_request_failure_releases_shared_lock(self):
        async def get(_client, url, **_kwargs):
            return httpx.Response(503, request=httpx.Request('GET', url))

        with patch.object(httpx.AsyncClient, 'get', get):
            with self.assertRaises(httpx.HTTPStatusError):
                await dc.fetch_dc_data('all', '202601')
        self.assertFalse(dc._DC_REQUEST_LOCK.locked())


if __name__ == '__main__':
    unittest.main()
