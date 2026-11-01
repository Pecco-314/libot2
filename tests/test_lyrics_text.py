import unittest

from src.common.lyrics_text import search_text, lyric_lines
from src.capture.fuzzy import LyricsSearchIndex, _score


class LyricsTextTests(unittest.TestCase):
    def test_multilingual_normalization(self):
        self.assertEqual(_score('ＬＯＶＥ，IS SOUR!', 'Love is sour'), 100)
        self.assertEqual(_score('我只想要和你慢慢老去', '我只想要 和你慢慢老去'), 100)
        self.assertEqual(_score('오늘만큼은', '오늘 만큼은'), 100)
        self.assertEqual(_score('君に 届くまで。', '君に届くまで'), 100)
        self.assertEqual(search_text('<|ja|>君に LOVE 사랑'), '君にlove사랑')
        self.assertEqual(search_text('Hello, WORLD!', compact=False), 'hello world')

    def test_metadata_removed_and_lines_preserved(self):
        text='[ti:歌]\n[00:00]作词：某人\n[00:02]第一句歌词\n[00:04]第二句歌词'
        self.assertEqual(lyric_lines(text), ['第一句歌词','第二句歌词'])
        long_old='作词：某人 '+ '第一句歌词 '*40
        self.assertEqual(lyric_lines(long_old), [long_old.strip()])

    def test_index_uses_search_view_without_changing_input(self):
        lyrics='[00:00]作曲：秘密名字\n[00:01]LOVE IS SOUR\n[00:03]오늘 만큼은'
        rows=[(1,'歌','','人','[]',1,'旧内容',lyrics)]
        index=LyricsSearchIndex(rows)
        result=index.search('Ｌｏｖｅ is sour!')[0]
        self.assertEqual(result['score'],100)
        self.assertEqual(result['lyrics_cleaned'],'LOVE IS SOUR\n오늘 만큼은')
        self.assertEqual(rows[0][-1],lyrics)
        self.assertEqual(index.search('...'),[])
