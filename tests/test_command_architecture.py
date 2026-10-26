"""命令模块边界、启动初始化及真实框架注册回归。"""

import ast
import subprocess
import sys
import textwrap
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from tests.command_sources import ROOT, command_source_files


class CommandArchitectureTests(unittest.TestCase):
    def test_feature_modules_do_not_depend_on_plugin_entry(self):
        for source in command_source_files():
            with self.subTest(module=source.name):
                tree = ast.parse(source.read_text())
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom):
                        self.assertNotEqual(node.module, "src.plugins.commands")
                        self.assertFalse(any(alias.name == "*" for alias in node.names))

    def test_feature_imports_are_acyclic(self):
        handlers = ROOT / "src" / "plugins" / "command_handlers"
        graph = {}
        prefix = "src.plugins.command_handlers."
        for source in handlers.glob("*.py"):
            graph[source.stem] = {
                node.module.removeprefix(prefix)
                for node in ast.walk(ast.parse(source.read_text()))
                if isinstance(node, ast.ImportFrom)
                and (node.module or "").startswith(prefix)
            }
        complete = set()

        def visit(module, chain):
            self.assertNotIn(module, chain, f"功能模块循环依赖：{[*chain, module]}")
            if module not in complete:
                for dependency in graph.get(module, ()):
                    visit(dependency, [*chain, module])
                complete.add(module)

        for module in graph:
            visit(module, [])

    def test_bot_startup_initializes_command_tables_and_propagates_errors(self):
        tree = ast.parse((ROOT / "src" / "plugins" / "config.py").read_text())
        init = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "init_all_db"
        )
        initializers = {
            node.func.id
            for node in ast.walk(init)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id.startswith("init_")
        }
        self.assertTrue(
            {"init_meme_db", "init_abbreviation_db", "init_live_list_db"}.issubset(
                initializers
            )
        )
        env = {name: Mock() for name in initializers}
        env["logger"] = SimpleNamespace(exception=Mock())
        exec(
            compile(ast.Module(body=[init], type_ignores=[]), "<startup>", "exec"), env
        )
        env["init_all_db"]()
        for name in initializers:
            env[name].assert_called_once_with()
        env["init_meme_db"].side_effect = RuntimeError("初始化失败")
        with self.assertRaisesRegex(RuntimeError, "初始化失败"):
            env["init_all_db"]()
        env["logger"].exception.assert_called_once()

    def test_real_nonebot_load_preserves_commands_and_cooldown_scope(self):
        program = textwrap.dedent("""
            import asyncio
            from types import SimpleNamespace
            from unittest.mock import AsyncMock, patch
            import nonebot
            from nonebot.adapters.onebot.v11 import Adapter
            from nonebot.matcher import Matcher
            from nonebot.rule import CommandRule

            nonebot.init(log_level="WARNING", command_start={"", "/"})
            nonebot.get_driver().register_adapter(Adapter)
            with patch("sqlite3.connect", side_effect=AssertionError("加载命令不得访问数据库")):
                plugin = nonebot.load_plugin("src.plugins.commands")
            assert plugin is not None
            from src.plugins import commands, command_registry as registry, command_policy as policy
            from src.plugins.command_handlers import settings, summary
            from src.db.command_config import CommandConfig

            matchers = [value for value in vars(registry).values()
                        if isinstance(value, type) and issubclass(value, Matcher)]
            assert len(matchers) == 59
            activity_rules = [checker.call for checker in registry.activity_list_cmd.rule.checkers
                              if isinstance(checker.call, CommandRule)]
            assert len(activity_rules) == 1
            assert set(activity_rules[0].cmds) == {("查动态",), ("生成动态",)}
            assert all(matcher.handlers for matcher in matchers)
            assert all(matcher.module_name == registry.COMMAND_MODULE_NAME for matcher in matchers)
            for matcher in matchers:
                checks = [checker.call for checker in matcher.rule.checkers
                          if isinstance(checker.call, CommandRule)]
                assert len(checks) == 1
                assert checks[0].force_whitespace is True
            assert registry._canonical_command_name("choose") == "抉择"
            for name in ("喜报", "悲报", "卖掉了"):
                assert registry._canonical_command_name(name) == name
            assert registry._canonical_command_name("/查大航海") == "查舰长"
            assert registry._canonical_command_name("粉丝团列表") == "粉丝团"
            assert registry._canonical_command_name("抽老婆") is None
            assert commands.handle_chat_summary is summary.handle_chat_summary
            assert commands.choose_cmd is registry.choose_cmd
            assert settings._COMMAND_CONFIG_CACHE is policy._COMMAND_CONFIG_CACHE
            assert settings._COMMAND_RATE_LIMITER is policy._COMMAND_RATE_LIMITER

            class Finished(Exception):
                pass

            async def check_cd():
                event = SimpleNamespace(get_user_id=lambda: "123")
                matcher = SimpleNamespace(module_name=registry.choose_cmd.module_name,
                                          finish=AsyncMock(side_effect=Finished))
                policy._COMMAND_CONFIG_CACHE[(1, "抉择")] = CommandConfig(cooldown_seconds=60)
                with patch.object(policy, "get_group_id", return_value=1), \\
                     patch.object(policy, "SUPERUSER", AsyncMock(return_value=False)), \\
                     patch.dict("os.environ", {"MANAGER_QQ": ""}):
                    await policy.apply_configured_command_cooldown(matcher, None, event, ("choose",))
                    try:
                        await policy.apply_configured_command_cooldown(matcher, None, event, ("抉择",))
                    except Finished:
                        pass
                    else:
                        raise AssertionError("真实注册命令未触发CD")
                    assert "CD中" in str(matcher.finish.call_args.args[0])
                    matcher.module_name = "other_plugin"
                    await policy.apply_configured_command_cooldown(matcher, None, event, ("choose",))
            asyncio.run(check_cd())
            print("command architecture verified")
        """)
        result = subprocess.run(
            [sys.executable, "-c", program],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("command architecture verified", result.stdout)
