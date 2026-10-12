from __future__ import annotations

import asyncio
import unittest
import ast
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from types import SimpleNamespace

from nonebot.adapters.onebot.v11 import Message, MessageSegment

from src.common.meme_image import extract_reply_text
from src.db.meme import is_valid_meme_name


class MemeReplyTest(unittest.TestCase):
    def test_extract_reply_text_ignores_non_text_segments(self) -> None:
        event = SimpleNamespace(
            reply=SimpleNamespace(
                message=Message(
                    [
                        MessageSegment.text("  回复里的内容  "),
                        MessageSegment.image("https://example.com/image.png"),
                    ]
                )
            )
        )
        self.assertEqual(extract_reply_text(event), "回复里的内容")

    def test_extract_reply_text_requires_plain_text(self) -> None:
        event = SimpleNamespace(
            reply=SimpleNamespace(
                message=Message(
                    [MessageSegment.image("https://example.com/image.png")]
                )
            )
        )
        self.assertIsNone(extract_reply_text(event))


class MemeAddRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_reply_tags_and_direct_content(self):
        node = next(n for n in ast.parse(Path('src/plugins/commands.py').read_text()).body
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == 'handle_meme_add')
        node.decorator_list = []
        class Finished(Exception):
            pass
        for replying, raw, expected_names, content in [
            (True, 'xyw 考子', ['xyw', '考子'], '回复正文'),
            (False, 'xyw 考子 更多 内容', ['xyw'], '考子 更多 内容'),
            (True, 'xyw', ['xyw'], '回复正文'),
        ]:
            with self.subTest(replying=replying, raw=raw):
                add = Mock(return_value=expected_names)
                async def to_thread(fn, *args):
                    return fn(*args)
                env = dict(Matcher=object, Event=object, CommandArg=lambda:None,
                           asyncio=SimpleNamespace(to_thread=to_thread),
                           extract_reply_text=lambda e: '回复正文',
                           is_valid_meme_name=is_valid_meme_name, add_meme_tags=add)
                exec(compile(ast.Module(body=[node],type_ignores=[]),'<handler>','exec'),env)
                event = SimpleNamespace(reply=object() if replying else None,get_user_id=lambda:'123')
                with self.assertRaises(Finished):
                    await env['handle_meme_add'](SimpleNamespace(finish=AsyncMock(side_effect=Finished)),
                        event, SimpleNamespace(extract_plain_text=lambda:raw))
                add.assert_called_once_with(expected_names,content,123)


class MemeDailyQueryRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_cheat_wife_uses_daily_draw_and_own_heading(self):
        node = next(n for n in ast.parse(Path('src/plugins/commands.py').read_text()).body
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == '_handle_meme_query')
        daily = Mock(return_value={'content': '测试内容', 'entry_type': 'text'})
        random = Mock()
        env = dict(
            Matcher=object, Bot=object, Event=object, asyncio=asyncio,
            Message=Message, MessageSegment=MessageSegment, _RANDOM_SONG_MEME_NAMES=set(),
            is_valid_meme_name=is_valid_meme_name,
            resolve_daily_meme_name=lambda name: '老婆.',
            get_daily_meme=daily, get_random_meme=random,
        )
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<meme-query>', 'exec'), env)

        class Finished(Exception):
            pass

        matcher = SimpleNamespace(finish=AsyncMock(side_effect=Finished))
        event = SimpleNamespace(get_user_id=lambda: '123')
        with self.assertRaises(Finished):
            await env['_handle_meme_query'](matcher, SimpleNamespace(self_id='456'), event, '老婆.')
        daily.assert_called_once_with('老婆.', 123)
        random.assert_not_called()
        message = matcher.finish.await_args.args[0]
        self.assertEqual(message[0].type, 'at')
        self.assertEqual(message[0].data['qq'], '123')
        self.assertEqual(message.extract_plain_text(), ' 你今天的老婆.是：\n测试内容')


if __name__ == "__main__":
    unittest.main()
