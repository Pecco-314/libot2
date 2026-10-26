"""动态查询的月份与日期路由。"""

import ast
from datetime import datetime
from pathlib import Path
import re
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from nonebot.adapters.onebot.v11 import Message
from src.common.time_args import parse_month


class ActivityCommandsTests(unittest.IsolatedAsyncioTestCase):
    async def test_month_and_date_dispatch(self):
        tree = ast.parse(Path("src/plugins/command_handlers/activities.py").read_text())
        nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name in {"_parse_activity_month", "handle_activity_list"}]
        for node in nodes:
            node.decorator_list = []
            node.args.defaults = []
            node.returns = None
            for arg in node.args.args:
                arg.annotation = None
        class Finished(Exception):
            pass
        for value in ("", "202610", "2026-10", "10", "2026-10-17", "2026-10-17 2"):
            with self.subTest(value=value):
                env = dict(datetime=datetime, re=re, parse_month=parse_month, get_query_room_id=Mock(return_value=1),
                           list_activities_by_month=Mock(return_value=[]),
                           handle_activity_generate=AsyncMock())
                exec(compile(ast.Module(body=nodes, type_ignores=[]), "<activities>", "exec"), env)
                matcher = SimpleNamespace(finish=AsyncMock(side_effect=Finished))
                bot, event, arg = object(), object(), Message(value)
                try:
                    await env["handle_activity_list"](matcher, bot, event, arg)
                except Finished:
                    pass
                if value.startswith("2026-10-17"):
                    env["handle_activity_generate"].assert_awaited_once_with(matcher, bot, event, arg)
                    env["list_activities_by_month"].assert_not_called()
                else:
                    env["list_activities_by_month"].assert_called_once()
                    env["handle_activity_generate"].assert_not_awaited()
