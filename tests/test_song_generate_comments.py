import ast
from datetime import datetime, timedelta
from pathlib import Path
import re
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
from src.common.time_args import normalize_date, is_date_arg


class SongGenerateCommentsTests(unittest.IsolatedAsyncioTestCase):
    async def test_comments_do_not_enter_search_or_output(self):
        tree = ast.parse(Path('src/plugins/command_handlers/song_management.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'handle_generate_list')
        node.decorator_list = []
        node.args.defaults = []
        for arg in node.args.args: arg.annotation = None
        search = Mock(return_value=[{'id': 1, 'title': '歌A', 'original_singer': '甲'}])
        command = SimpleNamespace(finish=AsyncMock())
        env = dict(datetime=datetime, timedelta=timedelta, re=re, cmd_generate_list=command, normalize_date=normalize_date, is_date_arg=is_date_arg,
                   search_songs_by_title=search)
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<generate>', 'exec'), env)
        text = '#注释\n2026-10-24 #开播日期\n歌A | 甲 #23:15:00\n#略过这一行\n歌A#第二遍'
        await env['handle_generate_list'](None, SimpleNamespace(extract_plain_text=lambda: text))
        self.assertEqual(search.call_count, 2)
        self.assertEqual(search.call_args_list[0].args, ('歌A',))
        self.assertEqual(search.call_args_list[0].kwargs['singer'], '甲')
        self.assertEqual(search.call_args_list[1].args, ('歌A',))
        command.finish.assert_awaited_once_with('[2026-10-24]\n1 # 歌A - 甲\n1 # 歌A - 甲')
