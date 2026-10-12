import ast
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
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
