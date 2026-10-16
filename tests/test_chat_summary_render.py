import ast
import asyncio
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from nonebot.adapters.onebot.v11 import MessageSegment
from PIL import Image

from src.common.chat_summary import attach_participants, summary_for_model
from src.common.chat_summary_service import parse_summary_query
from src.render.chat_summary import _display_text, render_summary
from tests.command_sources import command_tree


class ParticipantsTests(unittest.TestCase):
    def test_participants_are_actual_evidence_authors_and_local_only(self):
        result = {
            "topics": [
                {
                    "title": "讨论某主播",
                    "summary": "交流直播内容",
                    "message_ids": ["1", "2", "3", "4", "missing"],
                }
            ]
        }
        rows = [
            {
                "message_id": mid,
                "user_id": qq,
                "nickname": "旧名",
                "sent_at": 100,
                "segments": json.dumps([{"type": "text", "text": "讨论话题"}]),
            }
            for mid, qq in [(1, 12345), (2, 12345), (3, 23456), (4, 99999)]
        ]
        attach_participants(result, rows, {12345: {"name": "甲"}}, "99999")
        self.assertEqual(
            result["topics"][0]["participants"],
            [{"qq": 12345, "name": "甲"}, {"qq": 23456, "name": "旧名"}],
        )
        payload = json.dumps(summary_for_model(result))
        self.assertNotIn("12345", payload)
        self.assertNotIn("participants", payload)
        self.assertEqual(
            _display_text("12345认为好玩", result["topics"][0]["participants"]),
            "甲认为好玩",
        )


class RenderTests(unittest.TestCase):
    def test_multiple_pages_and_missing_avatar(self):
        result = {
            "period": "10-10 12:00—13:00",
            "message_count": 406,
            "topics": [
                {
                    "title": f"讨论话题{i}",
                    "summary": "群友讨论最近直播中的游戏与歌曲，也交流了自己的看法。"
                    * 18,
                    "participants": [
                        {"qq": 12345, "name": "甲"},
                        {"qq": 23456, "name": "乙"},
                    ],
                }
                for i in range(6)
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            paths = render_summary(result, {}, directory)
            self.assertGreater(len(paths), 1)
            for path in paths:
                with Image.open(path) as image:
                    self.assertEqual(image.width, 1000)
                    self.assertLess(image.height, 2000)
            self.assertEqual(render_summary(result, {}, directory), paths)


class ForwardTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_or_multiple_images_always_use_forward(self):
        node = next(
            n
            for n in command_tree().body
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "handle_chat_summary"
        )
        node.decorator_list = []
        for count in (1, 3):
            with self.subTest(count=count):
                send = AsyncMock()
                finish = AsyncMock(side_effect=AssertionError("正常结果应使用合并转发"))
                env = {
                    "Matcher": object,
                    "Bot": object,
                    "Event": object,
                    "CommandArg": lambda: SimpleNamespace(extract_plain_text=lambda: ''),
                    "asyncio": asyncio,
                    "get_command_config": lambda *args: SimpleNamespace(enabled=True),
                    "parse_summary_query": parse_summary_query,
                    "get_group_id": lambda event: 1,
                    "_CHAT_SUMMARY_BUSY": set(),
                    "_CHAT_SUMMARY_SLOTS": asyncio.Semaphore(2),
                    "count_summary": AsyncMock(return_value={}),
                    "render_summary_images": AsyncMock(
                        return_value=[f"/tmp/summary{i}.png" for i in range(count)]
                    ),
                    "MessageSegment": MessageSegment,
                    "send_forward_message": send,
                }
                exec(
                    compile(
                        ast.Module(body=[node], type_ignores=[]), "<handler>", "exec"
                    ),
                    env,
                )
                await env["handle_chat_summary"](
                    SimpleNamespace(finish=finish),
                    SimpleNamespace(self_id="99999"),
                    object(),
                )
                self.assertEqual(len(send.call_args.args[2]), count)
                self.assertTrue(
                    all(
                        item["data"]["content"].type == "image"
                        for item in send.call_args.args[2]
                    )
                )
                finish.assert_not_called()
