import ast
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from tests.command_sources import command_tree


class UserUidResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_numeric_unique_missing_and_ambiguous_queries(self):
        tree = command_tree()
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
        tree = command_tree()
        for name in ("handle_events", "handle_superchat", "handle_medal_wall"):
            with self.subTest(command=name):
                node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
                self.assertTrue(any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                                    and n.func.id == "_resolve_user_uid" for n in ast.walk(node)))

    async def test_name_history_displays_all_same_name_users(self):
        tree = command_tree()
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
            "Matcher": object, "Bot": object, "Event": object, "CommandArg": lambda: None,
            "MessageSegment": SimpleNamespace(text=lambda text: text),
            "send_forward_message": AsyncMock(),
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
                SimpleNamespace(finish=finish), SimpleNamespace(self_id="123"), object(),
                SimpleNamespace(extract_plain_text=lambda: "用户"),
            )
        lookup.assert_called_once_with("用户")
        message = finish.call_args.args[0]
        self.assertIn("找到2个用户", message)
        self.assertIn("新名一", message)
        self.assertIn("新名二", message)
        env["send_forward_message"].assert_not_called()

    def name_history_handler(self, history, names=None):
        node = next(n for n in command_tree().body if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == "handle_name_history")
        node.decorator_list = []

        async def to_thread(func, *args):
            return func(*args)

        env = {
            "Matcher": object, "Bot": object, "Event": object, "CommandArg": lambda: None,
            "asyncio": SimpleNamespace(to_thread=to_thread),
            "list_name_history_by_name_or_uid": Mock(return_value=history),
            "get_name_by_uid": AsyncMock(side_effect=names or [row["history"][-1] for row in history]),
            "MessageSegment": SimpleNamespace(text=lambda text: text),
            "send_forward_message": AsyncMock(), "logger": Mock(),
        }
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<history>", "exec"), env)
        return env

    async def test_name_history_folds_many_users_even_when_text_is_short(self):
        history = [{"uid": i, "history": ["旧名"]} for i in range(6)]
        env = self.name_history_handler(history)
        matcher = SimpleNamespace(finish=AsyncMock())
        bot, event = SimpleNamespace(self_id="123"), object()
        await env["handle_name_history"](
            matcher, bot, event, SimpleNamespace(extract_plain_text=lambda: "旧名")
        )
        matcher.finish.assert_not_called()
        env["send_forward_message"].assert_awaited_once()
        sent_bot, sent_event, nodes = env["send_forward_message"].call_args.args
        self.assertIs(sent_bot, bot)
        self.assertIs(sent_event, event)
        self.assertEqual(len(nodes), 1)
        self.assertIn("找到6个用户", nodes[0]["data"]["content"])
        self.assertIn("6. 旧名 (旧名)", nodes[0]["data"]["content"])

    async def test_name_history_folds_one_user_with_long_history(self):
        env = self.name_history_handler([{"uid": 1, "history": ["长名字" * 100]}], ["当前名"])
        matcher = SimpleNamespace(finish=AsyncMock())
        await env["handle_name_history"](
            matcher, SimpleNamespace(self_id="123"), object(),
            SimpleNamespace(extract_plain_text=lambda: "1"),
        )
        matcher.finish.assert_not_called()
        env["send_forward_message"].assert_awaited_once()

    async def test_name_history_chunks_preserve_all_text(self):
        names = ["账号已注销", "超长名字" * 500, "末尾名字"]
        history = [{"uid": i, "history": [name]} for i, name in enumerate(names)]
        env = self.name_history_handler(history)
        matcher = SimpleNamespace(finish=AsyncMock())
        await env["handle_name_history"](
            matcher, SimpleNamespace(self_id="123"), object(),
            SimpleNamespace(extract_plain_text=lambda: "账号已注销"),
        )
        matcher.finish.assert_not_called()
        nodes = env["send_forward_message"].call_args.args[2]
        chunks = [node["data"]["content"] for node in nodes]
        self.assertTrue(all(0 < len(chunk) <= 1500 for chunk in chunks))
        expected = "找到3个用户：\n" + "".join(
            f"{i}. {name} ({name})\n" for i, name in enumerate(names, 1)
        )
        self.assertEqual("".join(chunks), expected)

    async def test_name_history_forward_failure_does_not_dump_long_text(self):
        env = self.name_history_handler([{"uid": i, "history": ["账号已注销"]} for i in range(6)])
        env["send_forward_message"].side_effect = RuntimeError("unavailable")
        matcher = SimpleNamespace(finish=AsyncMock())
        await env["handle_name_history"](
            matcher, SimpleNamespace(self_id="123"), object(),
            SimpleNamespace(extract_plain_text=lambda: "账号已注销"),
        )
        matcher.finish.assert_awaited_once_with("曾用名结果发送失败，请稍后再试")
