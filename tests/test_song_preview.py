from datetime import datetime
from unittest.mock import Mock, patch
import unittest
from zoneinfo import ZoneInfo

from src.common.song_preview import SongPreviewCache, parse_preview_args, format_preview


def point(timestamp, title, score):
    return {'timestamp': timestamp, 'results': [{'title': title, 'singer': '歌手', 'final_score': score,
            'support_count': 4, 'asr_coverage': .8, 'lyrics_length': 100, 'lyric_spans': [[0, 30]],
            'matched_texts': ['一句', '二句', '三句', '四句'], 'evidence_times': [timestamp-20, timestamp+20],
            'preview_quality': .9}]}


class SongPreviewTests(unittest.TestCase):
    def test_short_gaps_merge_but_other_songs_and_long_gaps_split(self):
        def window(index, title=None, score=.99, singer='歌手'):
            results = point(1030 + index * 60, title, score)['results'] if title else []
            if results:
                results[0]['singer'] = singer
            return {'timestamp': 1030 + index * 60, 'window_start': 1000 + index * 60,
                    'window_end': 1060 + index * 60,
                    'results': results}
        cases = [
            ([window(0, 'A'), window(1), window(2, 'A')], ['A']),
            ([window(0, 'A'), window(1), window(2), window(3, 'A')], ['A']),
            ([window(0, 'A'), window(1), window(2), window(3), window(4, 'A')], ['A', 'A']),
            ([window(0, 'A'), window(1, 'B', .94), window(2, 'A')], ['A']),
            ([window(0, 'A'), window(1, 'B', .95), window(2, 'A')], ['A', 'B', 'A']),
            ([window(0, 'A'), window(1), window(2, 'A', singer='其他歌手')], ['A', 'A']),
        ]
        for timeline, expected in cases:
            with self.subTest(expected=expected, timeline=timeline):
                cache = SongPreviewCache(Mock(return_value=timeline))
                entries = cache.get(1, 1000, 1400, .95, now=2000)['entries']
                self.assertEqual([x['title'] for x in entries], expected)
                self.assertEqual(entries[0]['first_ts'], 1030)
                if len(entries) == 1:
                    self.assertEqual(entries[0]['sample_count'], 2)
                    self.assertEqual(entries[0]['last_ts'], timeline[-1]['timestamp'])

    def test_arguments(self):
        for text, expected in [('', (None, None, .85)), ('98%', (None, None, .98)),
                               ('0.98', (None, None, .98)),
                               ('昨天 98%', ('昨天', None, .98)),
                               ('2026-10-24 2', ('2026-10-24', 2, .85)),
                               ('2026-10-24 2 98', ('2026-10-24', 2, .98)),
                               ('2026-10-24 2 0.98', ('2026-10-24', 2, .98))]:
            self.assertEqual(parse_preview_args(text), expected)
        for text in ('100%', '-1%', '门槛=nan', '昨天 0', '昨天 2 99% extra'):
            with self.assertRaises(ValueError): parse_preview_args(text)

    def test_thresholds_share_raw_recognition(self):
        recognize = Mock(return_value=[point(1030, 'A', .96), point(1090, 'B', .99), point(1150, 'A', .97)])
        cache = SongPreviewCache(recognize)
        a = cache.get(1, 1000, 1300, .95, now=2000)
        b = cache.get(1, 1000, 1300, .98, now=2000)
        self.assertEqual([x['title'] for x in a['entries']], ['A', 'B', 'A'])
        self.assertEqual([x['title'] for x in b['entries']], ['B'])
        a['entries'].clear()
        self.assertEqual(len(cache.get(1, 1000, 1300, .95, now=2000)['entries']), 3)
        recognize.assert_called_once_with(1, 1000, 1300)

    def test_recent_tail_is_reserved(self):
        recognize = Mock(return_value=[])
        cache = SongPreviewCache(recognize)
        self.assertTrue(cache.get(1, 1000, None, .95, now=1100)['pending'])
        recognize.assert_not_called()
        cache.get(1, 1000, None, .95, now=1500)
        cache.get(1, 1000, None, .98, now=1510)
        recognize.assert_called_once_with(1, 1000, 1360)
        cache.get(1, 1000, None, .95, now=1570)
        self.assertEqual(recognize.call_args.args, (1, 1000, 1420))
        cache.get(1, 1000, 1500, .95, now=1800)
        self.assertEqual(recognize.call_args.args, (1, 1000, 1500))

    def test_expiry_recomputes_all_thresholds(self):
        recognize = Mock(return_value=[point(1030, 'A', .99)])
        cache = SongPreviewCache(recognize)
        with patch('src.common.song_preview.time.monotonic', return_value=0):
            cache.get(1, 1000, 1300, .95, now=2000)
            cache.get(1, 1000, 1300, .98, now=2000)
        recognize.return_value = [point(1030, 'B', .99)]
        with patch('src.common.song_preview.time.monotonic', return_value=301):
            self.assertEqual(cache.get(1, 1000, 1300, .98, now=2000)['entries'][0]['title'], 'B')
        self.assertEqual(recognize.call_count, 2)

    def test_format_keeps_start_date_and_repeats(self):
        start = int(datetime(2026, 10, 24, 23, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp())
        entries = [{'title':'歌A', 'singer':'甲', 'first_ts': start},
                   {'title':'歌B', 'singer':'乙', 'first_ts': start + 3600},
                   {'title':'歌A', 'singer':'甲', 'first_ts': start + 7200}]
        self.assertEqual(format_preview(start, entries), ['生成歌单 2026-10-24', '歌A | 甲 #23:00', '歌B | 乙 #00:00', '歌A | 甲 #01:00'])

    def test_threshold_is_inclusive(self):
        cache = SongPreviewCache(Mock(return_value=[point(1030, 'A', .95), point(1090, 'B', .949)]))
        self.assertEqual([x['title'] for x in cache.get(1, 1000, 1300, .95, now=2000)['entries']], ['A'])
