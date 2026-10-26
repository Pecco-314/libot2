import ast
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.capture import guesser
from src.common.song_time import parse_song_query_time


class NowPlayingTranslationTests(unittest.IsolatedAsyncioTestCase):
    def test_guesser_preserves_translation(self):
        info = {'title': 'Song', 'original_singer': 'Singer', 'title_trans': '译名'}
        with patch.object(guesser, '_ensure_matcher'), patch.object(
            guesser, '_score_text', return_value=[(1, info, .9)]
        ):
            self.assertEqual(guesser._guess_from_texts(['lyrics'])[0]['title_trans'], '译名')

    async def test_display_translation_and_empty_translation(self):
        tree = ast.parse(Path('src/plugins/command_handlers/songs.py').read_text())
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                    and n.name == 'handle_now_playing')
        node.decorator_list = []
        node.args.defaults = []
        for arg in node.args.args:
            arg.annotation = None
        for translation in ('译名', '', None, '  '):
            with self.subTest(translation=translation):
                env = dict(datetime=datetime, parse_song_query_time=parse_song_query_time,
                           get_query_room_id=lambda event: 1, logger=Mock(),
                           guess_song=lambda *args: [{'title':'Song', 'singer':'Singer',
                                                     'title_trans':translation, 'final_score':.9}])
                exec(compile(ast.Module(body=[node], type_ignores=[]), '<handler>', 'exec'), env)
                matcher = SimpleNamespace(finish=AsyncMock())
                await env['handle_now_playing'](matcher, None, SimpleNamespace(extract_plain_text=lambda: ''))
                expected = 'Song（译名）' if translation == '译名' else 'Song'
                matcher.finish.assert_awaited_once_with(f'当前可能在唱的歌曲：\n1. {expected} - Singer (90.00%)\n')
