import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.render.live_sessions import fetch_live_sessions_data


class LiveSessionsTest(unittest.IsolatedAsyncioTestCase):
    async def fetch(self, payload):
        response = MagicMock()
        response.json.return_value = payload
        client = AsyncMock()
        client.get.return_value = response
        with patch("src.render.live_sessions.httpx.AsyncClient") as factory:
            factory.return_value.__aenter__ = AsyncMock(return_value=client)
            factory.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await fetch_live_sessions_data(1967216004, "202609")
        client.get.assert_awaited_once_with(
            "https://vr.qianqiuzy.cn/gift/live_sessions",
            params={"room_id": 1967216004, "month": "202609"}, timeout=30,
        )
        return result

    async def test_finished_cross_midnight_session(self):
        result = await self.fetch({
            "room_id": 1967216004, "month": "202609", "sessions": [{
                "start_time": "2026-09-14 22:15:19", "end_time": "2026-09-15 05:39:05",
                "gift": 731.7, "guard": 414, "super_chat": 1324,
                "avg_concurrency": 676.84, "max_concurrency": 882,
            }],
        })
        session = result["sessions"][0]
        self.assertEqual(session["duration_minutes"], 443)
        self.assertAlmostEqual(session["total_revenue"], 2469.7)
        self.assertEqual(session["max_concurrency"], 882)

    async def test_unfinished_and_empty_sessions(self):
        result = await self.fetch({"room_id": 1967216004, "month": "202609", "sessions": [{
            "start_time": "2026-09-14 22:15:19", "end_time": None,
            "gift": None, "guard": 138,
        }]})
        self.assertEqual(result["sessions"][0]["duration_minutes"], 0)
        self.assertEqual(result["sessions"][0]["total_revenue"], 138)
        self.assertEqual((await self.fetch({"room_id": 1967216004, "month": "202609", "sessions": []}))["sessions"], [])

    async def test_reject_wrong_period(self):
        with self.assertRaisesRegex(ValueError, "月份"):
            await self.fetch({"room_id": 1967216004, "month": "202608", "sessions": []})


if __name__ == "__main__":
    unittest.main()
