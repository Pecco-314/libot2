import ast
import asyncio
import logging
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.common.qq_reactions import TZ, emoji_label, parse_period
from src.db import qq_reactions as db
from scripts.backfill_qq_reactions import recover


class ReactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "reactions.db"
        db.init_db(self.path)

    def notice(self, timestamp=100, uid=10, emoji="66", add=True, count=1, group=1, mid=1000, **extra):
        data = dict(self_id=9, group_id=group, user_id=uid, message_id=mid, time=timestamp,
                    is_add=add, likes=[dict(emoji_id=emoji, count=count)])
        data.update(extra)
        return db.store_notice(data, self.path)

    def rank(self, start=0, end=1000, received=False, group=1):
        return db.ranking(group, start, end, received, db_path=self.path)

    def test_cumulative_counts_and_duplicates(self):
        self.notice(count=500)
        self.notice(count=501)
        self.notice(timestamp=101, count=501)
        self.notice(uid=11, count=502)
        self.assertEqual([x["count"] for x in self.rank()["items"]], [1, 1])
        self.assertEqual(self.rank(start=101)["items"], [])

    def test_cancel_readd_and_historical_end(self):
        self.notice()
        self.notice(timestamp=200, add=False)
        self.assertEqual(self.rank(end=200)["total"], 1)
        self.assertEqual(self.rank(end=201)["total"], 0)
        self.notice(timestamp=300)
        self.assertEqual(self.rank(start=201)["total"], 1)
        self.assertEqual(self.rank(end=300)["total"], 0)

    def test_emoji_breakdown(self):
        self.notice()
        self.notice(emoji="128053")
        self.notice(mid=1001)
        self.assertEqual(self.rank()["items"][0]["emoji"], {"66": 2, "128053": 1})

    def test_default_ranking_shows_thirty_people(self):
        for uid in range(1, 36):
            self.notice(uid=uid)
        self.assertEqual(len(self.rank()["items"]), 30)

    def test_unknown_and_resolved_target(self):
        self.notice()
        self.assertEqual(self.rank(received=True)["items"], [])
        self.assertEqual(self.rank()["unknown"], 1)
        db.store_author(9, 1, 1000, 88, 50, "群友", db_path=self.path)
        self.assertEqual(self.rank(received=True)["items"][0]["name"], "群友")

    def test_group_and_bot_isolation(self):
        self.notice(group=2)
        self.assertEqual(self.rank()["total"], 0)
        db.store_author(8, 2, 1000, 88, db_path=self.path)
        self.assertEqual(self.rank(group=2)["unknown"], 1)

    def test_missing_actor_or_operation(self):
        self.assertEqual(self.notice(uid=0), 0)
        self.assertEqual(self.notice(add=None), 0)

    def test_same_second_operation_order(self):
        self.notice()
        self.notice(add=False)
        self.assertEqual(self.rank()["total"], 0)

    def test_cleanup_preserves_referenced_authors(self):
        self.notice()
        db.store_author(9, 1, 1000, 88, db_path=self.path)
        db.store_author(9, 1, 1001, 89, db_path=self.path)
        db.cleanup_authors(200, self.path)
        self.assertEqual(db.get_author(9, 1, 1000, self.path), 88)
        self.assertIsNone(db.get_author(9, 1, 1001, self.path))

    def test_log_import_idempotent(self):
        log = Path(self.tmp.name) / "libot.log"
        notice = dict(time=100, self_id=9, group_id=1, message_id=1000, user_id=10,
                      is_add=True, likes=[dict(emoji_id="66", count=8)])
        prefix = "09-24 10:00:00 [SUCCESS] nonebot | "
        log.write_text(prefix + "OneBot V11 9 | [message.group.normal]: Message 1000 from 88@[群:1]\n"
                       + prefix + f"OneBot V11 9 | [notice.group_msg_emoji_like]: {notice!r}\n"
                       + prefix + f"OneBot V11 9 | [message.group.normal]: Message 3 from 88@[群:1] 'OneBot V11 9 | [notice.group_msg_emoji_like]: {notice!r}'\n")
        self.assertEqual(recover([log])["notices"], 1)
        self.assertEqual(recover([log])["author_resolved_notices"], 1)
        self.assertEqual(recover([log], apply=True, db_path=self.path)["inserted"], 1)
        self.assertEqual(recover([log], apply=True, db_path=self.path)["inserted"], 0)
        self.assertEqual(self.rank(received=True)["items"][0]["uid"], 88)


class PeriodTests(unittest.TestCase):
    now = datetime(2026, 9, 24, 12, 0, tzinfo=TZ)

    def test_default(self):
        start, end, _ = parse_period("", self.now)
        self.assertEqual(start, int(datetime(2026, 9, 1, tzinfo=TZ).timestamp()))
        self.assertEqual(end, int(self.now.timestamp())+1)

    def test_inclusive_dates(self):
        start, end, _ = parse_period("2026-09-01 2026-09-02", self.now)
        self.assertEqual(end-start, 2*86400)

    def test_periods(self):
        for raw, expected in [("昨天", 86400), ("2026-08", 31*86400), ("2026-09-02", 86400)]:
            start, end, _ = parse_period(raw, self.now)
            self.assertEqual(end-start, expected)
        start, end, _ = parse_period("近7天", self.now)
        self.assertEqual(datetime.fromtimestamp(start, TZ).day, 18)

    def test_invalid(self):
        for raw in ["2026-09-31", "2026-09-24 2026-09-01", "2027-01", "近0天", "0天", "-7天", "10000天", "1 2 3"]:
            with self.assertRaises(ValueError):
                parse_period(raw, self.now)

    def test_days_without_prefix(self):
        for days in (1, 7, 30, 365, 9999):
            with self.subTest(days=days):
                self.assertEqual(parse_period(f"{days}天", self.now),
                                 parse_period(f"近{days}天", self.now))
        self.assertEqual(parse_period("1天", self.now), parse_period("今天", self.now))
        self.assertEqual(parse_period(" 7天 ", self.now), parse_period("近7天", self.now))

    def test_emoji_names(self):
        self.assertEqual(emoji_label("66"), "[爱心]")
        self.assertEqual(emoji_label("128053"), "🐵")
        self.assertEqual(emoji_label("99999999"), "[表情99999999]")


class RealtimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tree = ast.parse(Path("src/plugins/qq_reactions.py").read_text())
        funcs = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)]
        for node in funcs:
            node.decorator_list = []
        self.db = SimpleNamespace(store_author=Mock(), store_notice=Mock(), get_author=Mock(return_value=None))
        self.env = dict(asyncio=asyncio, time=time, db=self.db, logger=logging.getLogger("test"),
                        Bot=object, GroupMessageEvent=object, NoticeEvent=object,
                        _lookups=asyncio.Semaphore(2), _pending=set(), _misses={})
        exec(compile(ast.Module(body=funcs, type_ignores=[]), "qq_reactions.py", "exec"), self.env)

    def event(self):
        from nonebot.adapters.onebot.v11 import NoticeEvent
        return NoticeEvent(time=100, self_id=9, post_type="notice", notice_type="group_msg_emoji_like",
                           group_id=1, message_id=2, user_id=3, is_add=True,
                           likes=[{"emoji_id": "66", "count": 100}])

    async def test_event_extras_and_cached_author(self):
        self.db.get_author.return_value = 88
        bot = SimpleNamespace(get_msg=AsyncMock())
        await self.env["remember_reaction"](bot, self.event())
        self.assertEqual(self.db.store_notice.call_args.args[0]["likes"][0]["count"], 100)
        bot.get_msg.assert_not_called()

    async def test_unknown_target_recovery(self):
        bot = SimpleNamespace(get_msg=AsyncMock(return_value={"group_id": 1, "message_id": 2,
            "time": 80, "sender": {"user_id": 88, "nickname": "群友"}}))
        await self.env["remember_reaction"](bot, self.event())
        self.db.store_author.assert_called_once_with(9, 1, 2, 88, 80, "群友")

    async def test_mismatched_group_is_not_assigned(self):
        bot = SimpleNamespace(get_msg=AsyncMock(return_value={"group_id": 77, "message_id": 2,
            "sender": {"user_id": 88}}))
        await self.env["remember_reaction"](bot, self.event())
        self.db.store_author.assert_not_called()
        await self.env["remember_reaction"](bot, self.event())
        self.assertEqual(bot.get_msg.await_count, 1)

    async def test_failed_lookup_still_records_notice(self):
        bot = SimpleNamespace(get_msg=AsyncMock(side_effect=RuntimeError("not found")))
        await self.env["remember_reaction"](bot, self.event())
        self.db.store_notice.assert_called_once()
        self.db.store_author.assert_not_called()
        self.assertFalse(self.env["_pending"])


class RenderTests(unittest.TestCase):
    def test_thirty_people_fit_one_image_without_ids_or_footer(self):
        from src.render.qq_reactions import render_rank, text_image
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            result = {"items": [{"uid": 777770+i, "name": "测试群友", "count": 123,
                                  "emoji": {"66": 120, "128053": 3}} for i in range(30)],
                      "first": 1788880768, "unknown": 100}
            with patch("src.render.qq_reactions.text_image", wraps=text_image) as text:
                paths = render_rank(result, False, "2026-09-01 ～ 2026-09-24", Path(directory))
            rendered = "\n".join(call.args[0] for call in text.call_args_list)
            for item in result["items"]:
                self.assertNotIn(str(item["uid"]), rendered)
            self.assertNotIn("最早记录", rendered)
            self.assertNotIn("未确认", rendered)
            self.assertNotIn("同人", rendered)
            self.assertEqual(len(paths), 1)
            with Image.open(paths[0]) as img:
                self.assertEqual(img.width, 1680)
                self.assertLess(img.height, 7400)

    def test_only_top_ten_emojis_and_full_total(self):
        from src.render.qq_reactions import row_image, emoji_chip, text_image
        item = {"uid": 9999999, "name": "9999999", "count": 120,
                "emoji": {str(i): i+1 for i in range(15)}}
        with patch("src.render.qq_reactions.emoji_chip", wraps=emoji_chip) as chips, \
             patch("src.render.qq_reactions.text_image", wraps=text_image) as text:
            row_image(1, item, 788)
        self.assertEqual([call.args[0] for call in chips.call_args_list], [str(i) for i in range(14,4,-1)])
        heading = text.call_args_list[0].args[0]
        self.assertIn("120次", heading)
        self.assertIn("未知昵称", heading)
        self.assertNotIn("9999999", heading)

    def test_name_fallback_and_icon(self):
        from src.render.qq_reactions import emoji_chip
        from PIL import Image
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "66.png"
            with patch("src.render.qq_reactions.cache_path", return_value=path):
                self.assertGreater(emoji_chip("66", 10).width, 32)
                Image.new("RGBA", (48, 48), "red").save(path)
                chip = emoji_chip("66", 10)
                self.assertEqual(chip.getpixel((16, 20))[:3], (255, 0, 0))


if __name__ == "__main__":
    unittest.main()
