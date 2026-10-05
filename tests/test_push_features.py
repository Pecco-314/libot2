import ast
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from src.common.push_features import OWN_ROOM_ACTIVITY, OTHER_ROOM_ACTIVITY, PUSH_FEATURE_ALIASES, room_activity_feature


def load_functions(path, names, env):
    tree = ast.parse(Path(path).read_text())
    funcs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    for node in funcs:
        node.decorator_list = []
    exec(compile(ast.Module(body=funcs, type_ignores=[]), path, "exec"), env)
    return env


class PushFeatureTests(unittest.IsolatedAsyncioTestCase):
    async def test_own_room_group_gate_does_not_affect_live_notices(self):
        bot = SimpleNamespace(call_api=AsyncMock())
        check = Mock(side_effect=lambda group, feature: group == 2)
        env = load_functions("src/plugins/scheduler.py", {"send_to_room"}, {
            "asyncio": asyncio, "get_bots": lambda: {1:bot},
            "list_subscribed_group_ids": lambda room: [1,2], "is_push_enabled": check,
            "logger": logging.getLogger("test")})
        await env["send_to_room"](100, "弹幕", feature=OWN_ROOM_ACTIVITY)
        self.assertEqual([c.kwargs["group_id"] for c in bot.call_api.await_args_list], [2])
        bot.call_api.reset_mock()
        check.reset_mock()
        await env["send_to_room"](100, "开播通知")
        self.assertEqual([c.kwargs["group_id"] for c in bot.call_api.await_args_list], [1,2])
        check.assert_not_called()

    def test_feature_names_and_aliases(self):
        env = load_functions("src/plugins/commands.py", {"_parse_feature_name"}, {
            "PUSH_FEATURE_ALIASES": PUSH_FEATURE_ALIASES, "RESERVED_FEATURE_REGISTRY": {},
            "_canonical_command_name": lambda name: None})
        parse = env["_parse_feature_name"]
        self.assertEqual(parse("本房互动转发"), OWN_ROOM_ACTIVITY)
        self.assertEqual(parse("本房弹幕转发"), OWN_ROOM_ACTIVITY)
        self.assertEqual(parse("外房互动转发"), OTHER_ROOM_ACTIVITY)
        self.assertEqual(parse("跨房互动转发"), OTHER_ROOM_ACTIVITY)
        self.assertEqual(room_activity_feature(100,100), OWN_ROOM_ACTIVITY)
        self.assertEqual(room_activity_feature(200,100), OTHER_ROOM_ACTIVITY)


if __name__ == "__main__":
    unittest.main()
