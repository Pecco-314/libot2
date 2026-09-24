import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from src.db.command_config import CommandConfig
from src.plugins.command_rate_limit import CommandRateLimiter


class CommandRateLimitTests(unittest.TestCase):
    def test_sliding_window_and_single_notification(self):
        limiter = CommandRateLimiter()
        key = (1, "来点", 2)
        for now in (0, 10, 20):
            self.assertEqual(limiter.check(key, 180, 3, now), (True, False))
        self.assertEqual(limiter.check(key, 180, 3, 30), (False, True))
        self.assertEqual(limiter.check(key, 180, 3, 40), (False, False))
        self.assertEqual(limiter.check(key, 180, 3, 180), (True, False))
        self.assertEqual(limiter.check(key, 180, 3, 181), (False, True))
        for other in ((2, "来点", 2), (1, "抉择", 2), (1, "来点", 3)):
            self.assertEqual(limiter.check(other, 180, 3, 181), (True, False))
        limiter.reset(1, "来点")
        self.assertEqual(limiter.check(key, 180, 3, 182), (True, False))

    def test_real_preprocessor_command_scope(self):
        # 仅提取预处理函数，避免加载生产插件及其数据库初始化。
        tree = ast.parse(Path("src/plugins/commands.py").read_text())
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == "apply_configured_command_cooldown")
        node.decorator_list = []
        node.args.defaults = []
        for arg in node.args.args:
            arg.annotation = None
        limiter = CommandRateLimiter()
        env = {
            "__name__": "test_commands", "asyncio": asyncio,
            "get_group_id": lambda event: event.group_id,
            "_canonical_command_name": lambda name: {"来点": "来点", "别名": "来点"}.get(name),
            "_COMMAND_CONFIG_CACHE": {(1, "来点"): CommandConfig(cooldown_seconds=180, cooldown_count=3)},
            "_COMMAND_RATE_LIMITER": limiter,
            "SUPERUSER": AsyncMock(return_value=False),
            "time": SimpleNamespace(monotonic=lambda: 10),
            "_format_duration": lambda seconds: "3分钟",
            "Message": lambda parts: parts,
            "MessageSegment": SimpleNamespace(at=lambda uid: f"@{uid}", text=lambda text: text),
        }
        exec(compile(ast.Module(body=[node], type_ignores=[]), "commands.py", "exec"), env)

        class Finished(Exception):
            pass

        messages = []

        async def finish(message=None):
            messages.append(message)
            raise Finished

        matcher = SimpleNamespace(module_name="test_commands", finish=finish)
        event = SimpleNamespace(group_id=1, get_user_id=lambda: "2")

        async def run():
            call = env["apply_configured_command_cooldown"]
            for name in ("来点", "别名", "来点"):
                await call(matcher, None, event, (name,))
            for _ in range(2):
                with self.assertRaises(Finished):
                    await call(matcher, None, event, ("来点",))
            self.assertEqual(messages, [["@2", " CD中（当前CD：3分钟3次）"], None])
            env["SUPERUSER"].return_value = True
            await call(matcher, None, event, ("来点",))
            matcher.module_name = "another_plugin"
            await call(matcher, None, event, ("来点",))

        asyncio.run(run())
