import ast
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, Mock
from typing import Any

from src.common.song_query import parse_song_query


class SongQueryTests(unittest.TestCase):
    def test_counts_and_titles(self):
        for text, expected in [('半梦', ('半梦', 5)),
                               ('#10 半梦', ('半梦', 10)),
                               ('#max Say So', ('Say So', None)),
                               ('#MAX 半梦', ('半梦', None))]:
            self.assertEqual(parse_song_query(text), expected)
        for text in ['', '#0 半梦', '#-1 半梦', '#max', '#10半梦', '#abc 半梦']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_song_query(text)

class SongBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_image_per_song_with_all_records(self):
        tree = ast.parse(Path('src/render/song.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == 'render_songs_by_keyword')
        song = dict(id=1, title='半梦', count=102, records=['2026-10-28'] * 102)
        save = Mock(return_value={'image_path': 'song.png', 'data': song})
        env = dict(Any=Any, search_songs_by_title=Mock(return_value=[song]),
                   prefetch_emoji_assets=AsyncMock(), save_song_card=save)
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<render>', 'exec'), env)
        for limit in (5, 10, None):
            save.reset_mock()
            result = await env['render_songs_by_keyword']('半梦', record_limit=limit)
            self.assertEqual(len(result), 1)
            save.assert_called_once_with(song, record_limit=limit)

    async def test_only_hash_can_follow_command_without_whitespace(self):
        tree = ast.parse(Path('src/plugins/command_registry.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == '_song_query_boundary')
        node.args.defaults = []
        env = {}
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<boundary>', 'exec'), env)
        class Arg(str):
            def extract_plain_text(self):
                return str(self)
        check = env['_song_query_boundary']
        self.assertTrue(await check(Arg('#10 半梦'), None))
        self.assertTrue(await check(Arg('#max 半梦'), None))
        self.assertTrue(await check(Arg('半梦'), ' '))
        self.assertFalse(await check(Arg('半梦'), None))
