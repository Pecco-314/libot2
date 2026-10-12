import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, AsyncMock


class UserUidResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_numeric_unique_missing_and_ambiguous_queries(self):
        tree = ast.parse(Path("src/plugins/commands.py").read_text())
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == "_resolve_user_uid")
        lookup = Mock()
        async def to_thread(func, *args):
            return func(*args)
        env = {"Matcher": object, "asyncio": SimpleNamespace(to_thread=to_thread), "list_uids_by_uname": lookup}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<resolver>", "exec"), env)

        class Finished(Exception):
            pass

        finish = AsyncMock(side_effect=Finished)
        matcher = SimpleNamespace(finish=finish)
        resolve = env["_resolve_user_uid"]
        self.assertEqual(await resolve(matcher, "99"), 99)
        lookup.assert_not_called()
        lookup.return_value = [11]
        self.assertEqual(await resolve(matcher, "用户"), 11)
        lookup.return_value = []
        with self.assertRaises(Finished):
            await resolve(matcher, "不存在")
        self.assertIn("未找到用户", finish.call_args.args[0])
        lookup.return_value = [11, 22]
        with self.assertRaises(Finished):
            await resolve(matcher, "用户")
        message = finish.call_args.args[0]
        self.assertIn("请改用UID", message)
        self.assertIn("用户（11）", message)
        self.assertIn("用户（22）", message)
        lookup.return_value = list(range(1, 15))
        with self.assertRaises(Finished):
            await resolve(matcher, "用户")
        self.assertTrue(finish.call_args.args[0].endswith("……"))

    def test_user_query_commands_use_shared_resolver(self):
        tree = ast.parse(Path("src/plugins/commands.py").read_text())
        for name in ("handle_events", "handle_superchat", "handle_medal_wall"):
            with self.subTest(command=name):
                node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
                self.assertTrue(any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                                    and n.func.id == "_resolve_user_uid" for n in ast.walk(node)))

    async def test_name_history_displays_all_same_name_users(self):
        tree = ast.parse(Path("src/plugins/commands.py").read_text())
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == "handle_name_history")
        node.decorator_list = []
        lookup = Mock(return_value=[
            {"uid": 11, "history": ["用户", "新名一"]},
            {"uid": 22, "history": ["用户", "新名二"]},
        ])
        async def to_thread(func, *args):
            return func(*args)
        env = {
            "Matcher": object, "Event": object, "CommandArg": lambda: None,
            "asyncio": SimpleNamespace(to_thread=to_thread),
            "list_name_history_by_name_or_uid": lookup,
            "get_name_by_uid": AsyncMock(side_effect=["新名一", "新名二"]),
        }
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<history>", "exec"), env)
        class Finished(Exception):
            pass
        finish = AsyncMock(side_effect=Finished)
        with self.assertRaises(Finished):
            await env["handle_name_history"](
                SimpleNamespace(finish=finish), object(),
                SimpleNamespace(extract_plain_text=lambda: "用户"),
            )
        lookup.assert_called_once_with("用户")
        message = finish.call_args.args[0]
        self.assertIn("找到2个用户", message)
        self.assertIn("新名一", message)
        self.assertIn("新名二", message)
