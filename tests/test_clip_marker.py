import ast
import re
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

from src.db.clip_marker import add_clip_marker, format_clip_marker, list_clip_markers
from tests.command_sources import command_tree


class ClipMarkerTest(unittest.TestCase):
    def test_timeline_across_midnight(self):
        zone = ZoneInfo("Asia/Shanghai")
        start = int(datetime(2026, 9, 15, 23, 0, tzinfo=zone).timestamp())
        timestamp = start + 3661
        self.assertEqual(format_clip_marker(timestamp, start, "高光\n时刻"),
                         "[00:01:01] (01:01:01) 高光 时刻")
        self.assertIn("(25:00:00)", format_clip_marker(start + 90000, start, ""))

    def test_order_scope_and_duplicate_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "test.db"
            add_clip_marker(1, 100, 130, 10, 20, 1, "后", db)
            add_clip_marker(1, 100, 120, 11, 21, 2, "先", db)
            add_clip_marker(1, 100, 130, 10, 20, 1, "重复", db)
            add_clip_marker(2, 100, 140, 10, 20, 3, "其他房间", db)
            add_clip_marker(1, 200, 210, 10, 20, 4, "其他场次", db)
            self.assertEqual(list_clip_markers(1, 100, db), [
                {"timestamp": 120, "note": "先"}, {"timestamp": 130, "note": "后"}])
            with self.assertRaises(ValueError):
                add_clip_marker(1, 100, 99, 10, 20, 5, "过早", db)


class Finished(Exception):
    pass


class ClipMarkerCommandTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        nodes = [node for node in command_tree().body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and node.name in {"_parse_clip_marker_arg", "handle_clip_marker"}]
        for node in nodes:
            node.decorator_list = []

        async def inline_thread(function, *args):
            return function(*args)

        self.now = self.ts("2026-10-14 00:30:00")
        self.start = self.ts("2026-10-13 23:00:00")
        self.sessions = [{"start_ts": self.start, "end_ts": self.now - 60, "ongoing": False}]
        self.add = Mock()
        self.latest = Mock(return_value=self.sessions[0])
        self.matcher = SimpleNamespace(finish=AsyncMock(side_effect=Finished))
        self.event = SimpleNamespace(time=self.now, user_id=10, group_id=20, message_id=30)
        env = dict(re=re, datetime=datetime, timedelta=timedelta, ZoneInfo=ZoneInfo,
                   Matcher=object, Event=object, GroupMessageEvent=SimpleNamespace,
                   CommandArg=lambda: None, asyncio=SimpleNamespace(to_thread=inline_thread),
                   get_query_room_id=lambda event: 1, get_latest_live_session=self.latest,
                   list_live_sessions=lambda room: self.sessions, add_clip_marker=self.add,
                   format_clip_marker=format_clip_marker)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "clip_marker_handler", "exec"), env)
        self.parse = env["_parse_clip_marker_arg"]
        self.handler = env["handle_clip_marker"]

    @staticmethod
    def ts(value):
        return int(datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
                   .replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())

    async def run_handler(self, text):
        with self.assertRaises(Finished):
            await self.handler(self.matcher, self.event,
                               SimpleNamespace(extract_plain_text=lambda: text))

    def test_time_forms_and_previous_day(self):
        for text, expected in [
            ("高光 2026-10-13 23:30:12", "2026-10-13 23:30:12"),
            ("高光 2026-10-13 23:30", "2026-10-13 23:30:00"),
            ("高光 23:30", "2026-10-13 23:30:00"),
            ("高光 00:20:12", "2026-10-14 00:20:12"),
            ("高光 00:30:00", "2026-10-14 00:30:00"),
        ]:
            with self.subTest(text=text):
                self.assertEqual(self.parse(text, self.now), ("高光", self.ts(expected)))
        self.assertEqual(self.parse("精彩  多人\n合唱", self.now), ("精彩 多人 合唱", None))
        self.assertEqual(self.parse("高光 23:59", self.ts("2026-01-01 00:01:00")),
                         ("高光", self.ts("2025-12-31 23:59:00")))

    async def test_historical_cross_midnight_session(self):
        await self.run_handler("精彩 合唱 2026-10-14 00:20:00")
        self.add.assert_called_once_with(1, self.start, self.ts("2026-10-14 00:20:00"),
                                         10, 20, 30, "精彩 合唱")
        self.assertIn("(01:20:00)", self.matcher.finish.call_args.args[0])

    async def test_time_only_previous_day(self):
        await self.run_handler("高光 23:30")
        self.assertEqual(self.add.call_args.args[2], self.ts("2026-10-13 23:30:00"))

    async def test_no_matching_session_and_end_boundary(self):
        for text in ["高光 22:00", "高光 00:29:00"]:
            await self.run_handler(text)
            self.matcher.finish.assert_awaited_with("指定时间没有对应的直播记录")
        self.add.assert_not_called()

    async def test_invalid_and_future_times_do_not_write(self):
        for text in ["高光 25:00", "高光 12:60", "高光 2026-02-30 12:00",
                     "高光 2026-10-14 23:00", "高光 2026-10-13"]:
            await self.run_handler(text)
        self.add.assert_not_called()
        self.latest.assert_not_called()

    async def test_realtime_requires_ongoing_session(self):
        await self.run_handler("高光")
        self.matcher.finish.assert_awaited_with("当前没有正在进行的直播")
        self.add.assert_not_called()
        self.latest.return_value = {"start_ts": self.start, "end_ts": None, "ongoing": True}
        await self.run_handler("高光")
        self.add.assert_called_once_with(1, self.start, self.now, 10, 20, 30, "高光")

    async def test_note_limit_excludes_time_suffix(self):
        await self.run_handler("字" * 500 + " 23:30")
        self.add.assert_called_once()
        self.add.reset_mock()
        await self.run_handler("字" * 501 + " 23:30")
        self.matcher.finish.assert_awaited_with("切片备注最多 500 字")
        self.add.assert_not_called()


class ClipDocumentTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tree = command_tree()
        handler = next(node for node in tree.body
                       if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_clip_document")
        handler.decorator_list = []

        async def inline_thread(function, *args):
            return function(*args)

        self.start = 1_800_000_000
        self.markers = []
        self.forward = AsyncMock()
        self.matcher = SimpleNamespace(finish=AsyncMock(side_effect=Finished))
        self.bot = SimpleNamespace(self_id=123)
        self.event = object()
        env = dict(Matcher=object, Bot=object, Event=object, CommandArg=lambda: None,
                   datetime=datetime, asyncio=SimpleNamespace(to_thread=inline_thread),
                   MessageSegment=SimpleNamespace(text=lambda text: text),
                   get_query_room_id=lambda event: 1,
                   _resolve_live_session=lambda *args: ({"start_ts": self.start}, "最近直播", False),
                   list_clip_markers=lambda *args: self.markers,
                   format_clip_marker=format_clip_marker, send_forward_message=self.forward)
        exec(compile(ast.Module(body=[handler], type_ignores=[]), "clip_document_handler", "exec"), env)
        self.handler = env["handle_clip_document"]

    async def run_handler(self, notes):
        self.markers = [{"timestamp": self.start + i * 60, "note": note}
                        for i, note in enumerate(notes)]
        try:
            await self.handler(self.matcher, self.bot, self.event,
                               SimpleNamespace(extract_plain_text=lambda: ""))
        except Finished:
            pass

    async def test_short_document_direct(self):
        await self.run_handler(["高光"] * 5)
        self.matcher.finish.assert_awaited_once()
        self.forward.assert_not_awaited()

    async def test_six_markers_fold_even_with_short_notes(self):
        await self.run_handler([""] * 6)
        self.matcher.finish.assert_not_awaited()
        self.forward.assert_awaited_once()
        bot, event, nodes = self.forward.call_args.args
        self.assertIs(bot, self.bot)
        self.assertIs(event, self.event)
        self.assertEqual(len(nodes), 1)
        self.assertIn("[北京时间] (开播后时间) 备注", nodes[0]["data"]["content"])

    async def test_character_threshold(self):
        await self.run_handler(["字"])
        baseline = len(self.matcher.finish.call_args.args[0]) - 1
        self.matcher.finish.reset_mock()
        await self.run_handler(["字" * (300 - baseline)])
        self.matcher.finish.assert_awaited_once()
        self.forward.assert_not_awaited()
        self.matcher.finish.reset_mock()
        await self.run_handler(["字" * (301 - baseline)])
        self.matcher.finish.assert_not_awaited()
        self.forward.assert_awaited_once()

    async def test_long_document_keeps_all_markers_in_order(self):
        notes = [f"切片{i}" + "字" * 490 for i in range(10)]
        await self.run_handler(notes)
        nodes = self.forward.call_args.args[2]
        self.assertGreater(len(nodes), 1)
        contents = [node["data"]["content"] for node in nodes]
        self.assertTrue(all(len(text) <= 1500 for text in contents))
        expected = [format_clip_marker(m["timestamp"], self.start, m["note"]) for m in self.markers]
        self.assertEqual("\n".join(contents).splitlines()[3:], expected)

    async def test_empty_document_direct(self):
        await self.run_handler([])
        self.matcher.finish.assert_awaited_once_with("最近直播暂无切片记录")
        self.forward.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
