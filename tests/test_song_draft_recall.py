import ast
from datetime import datetime
from pathlib import Path
import re
import os
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
from nonebot.adapters.onebot.v11 import Message
from src.common.song_submission import parse_song_submission


class SongDraftRecallTests(unittest.IsolatedAsyncioTestCase):
    async def run_submission(self, *, sender=123, draft='[2026-10-16]\n1 # 歌名 - 歌手',
                             save_error=False, recall_error=False, reply=True, role='admin', permission_error=False):
        source = Path('src/plugins/command_handlers/song_management.py').read_text()
        node = next(n for n in ast.parse(source).body
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == 'handle_update_list')
        node.decorator_list = []
        node.args.defaults = []
        for arg in node.args.args:
            arg.annotation = None
        message = Message
        event = SimpleNamespace(reply=SimpleNamespace(message=message(draft),
                                sender=SimpleNamespace(user_id=sender), message_id=456) if reply else None,
                                get_user_id=lambda: '9999999', group_id=1)
        bot = SimpleNamespace(self_id='123', delete_msg=AsyncMock(
            side_effect=RuntimeError('expired') if recall_error else None),
            get_group_member_info=AsyncMock(return_value={'role': role},
                                           side_effect=RuntimeError() if permission_error else None))
        command = SimpleNamespace(finish=AsyncMock())
        def save(text):
            parse_song_submission(text)
            if save_error:
                raise ValueError('failed')
        ns = dict(datetime=datetime, re=re, os=os, asyncio=asyncio, SUPERUSER=AsyncMock(return_value=False),
                  submit_song_list=save, cmd_update_list=command, logger=Mock())
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<handler>', 'exec'), ns)
        await ns['handle_update_list'](bot, event, message('' if reply else draft))
        return bot, command

    async def test_success_recalls_only_replied_bot_draft(self):
        bot, command = await self.run_submission()
        bot.delete_msg.assert_awaited_once_with(message_id=456)
        command.finish.assert_awaited_once_with('更新成功！')

    async def test_failure_or_other_message_keeps_draft(self):
        for options in ({'save_error': True}, {'sender': 999}, {'reply': False},
                        {'draft': '[2026-10-16]\n1 # song\n? # unknown'},
                        {'draft': '[2026-10-16]'}):
            with self.subTest(options=options):
                bot, _ = await self.run_submission(**options)
                bot.delete_msg.assert_not_awaited()

    async def test_recall_failure_does_not_fail_submission(self):
        bot, command = await self.run_submission(recall_error=True)
        command.finish.assert_awaited_once_with('更新成功！')

    async def test_permissions_fail_closed(self):
        for options in ({'role': 'member'}, {'permission_error': True}):
            bot, command = await self.run_submission(**options)
            bot.delete_msg.assert_not_awaited()
            self.assertNotIn('更新成功', str(command.finish.call_args))
