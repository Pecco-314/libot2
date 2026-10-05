import ast
import logging
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock


class ActivityNotificationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tree = ast.parse(Path("src/plugins/scheduler.py").read_text())
        handler = next(n for n in tree.body
                       if isinstance(n, ast.AsyncFunctionDef) and n.name == "watch_activities")
        handler.decorator_list = []
        self.row = dict(id=42, activity_id="1234567890123456789", room_id=100,
                        uname="三理Mit3uri", timestamp=int(datetime.now().timestamp()),
                        dy_type_str="DYNAMIC_TYPE_DRAW")
        self.send = AsyncMock()
        self.state = Mock()
        env = dict(_ensure_last_activity_id_initialized=AsyncMock(),
                   _get_last_activity_id=lambda: 41, list_activities_after=lambda _: [self.row],
                   _render_activity_image=AsyncMock(return_value=Path("/tmp/activity.png")),
                   set_state=self.state, send_activity_to_room=self.send,
                   datetime=datetime, timedelta=timedelta, Message=list,
                   MessageSegment=SimpleNamespace(text=lambda text: ("text", text),
                                                  image=lambda **kw: ("image", kw["file"])),
                   LIVE_RCMD_ACTIVITY_TYPE="DYNAMIC_TYPE_LIVE_RCMD",
                   logger=logging.getLogger("test"))
        exec(compile(ast.Module(body=[handler], type_ignores=[]), "activity_handler", "exec"), env)
        self.handler = env["watch_activities"]

    async def test_link_uses_bilibili_id_and_follows_image(self):
        await self.handler()
        self.send.assert_awaited_once_with(100, [
            ("text", "三理Mit3uri发布了新动态！\n"),
            ("image", "/tmp/activity.png"),
            ("text", "\n(https://t.bilibili.com/1234567890123456789)"),
        ])
        self.state.assert_called_once_with("last_activity_id", 42)

    async def test_fallback_name(self):
        self.row["uname"] = None
        await self.handler()
        self.assertTrue(self.send.call_args.args[1][0][1].startswith("UP主发布了新动态！"))

    async def test_live_recommendation_stays_silent(self):
        self.row["dy_type_str"] = "DYNAMIC_TYPE_LIVE_RCMD"
        await self.handler()
        self.send.assert_not_awaited()
        self.state.assert_called_once_with("last_activity_id", 42)
