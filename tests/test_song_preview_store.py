import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.capture.guesser import guess_song_timeline
from src.common.song_preview import SongPreviewCache
from src.common.song_preview_store import PersistentPreviewRecognizer


class PreviewWindowCacheTests(unittest.TestCase):
    def test_extension_reuses_prefix_and_late_asr_recomputes_one_window(self):
        rows = [{'id': i, 'timestamp': ts, 'content': f'歌词{i}'}
                for i, ts in enumerate([1010, 1070, 1130, 1190])]
        windows = {}
        with patch('src.capture.guesser.list_transcripts_in_range', return_value=rows), patch('src.capture.guesser._guess_from_texts', return_value=[]) as guess:
            guess_song_timeline(1, 1000, 1180, window_cache=windows)
            self.assertEqual(guess.call_count, 3)
            guess_song_timeline(1, 1000, 1240, window_cache=windows)
            self.assertEqual(guess.call_count, 4)
            rows[1]['content'] = '补正歌词'
            guess_song_timeline(1, 1000, 1240, window_cache=windows)
            self.assertEqual(guess.call_count, 5)

    def test_partial_final_window_is_recomputed(self):
        windows = {}
        rows = [{'id': 1, 'timestamp': 1010, 'content': '歌词'}]
        with patch('src.capture.guesser.list_transcripts_in_range', return_value=rows), patch('src.capture.guesser._guess_from_texts', return_value=[]) as guess:
            guess_song_timeline(1, 1000, 1090, window_cache=windows)
            guess_song_timeline(1, 1000, 1120, window_cache=windows)
            self.assertEqual(guess.call_count, 3)

    def test_restart_reuses_disk_and_catalog_change_invalidates(self):
        rows = [{'id': 1, 'timestamp': 1010, 'content': '歌词'}]
        with tempfile.TemporaryDirectory() as folder, patch('src.capture.guesser.list_transcripts_in_range', return_value=rows), patch('src.capture.guesser._guess_from_texts', return_value=[]) as guess:
            first = PersistentPreviewRecognizer(folder)
            first.catalog = 'v1'
            first(1, 1000, 1060)
            second = PersistentPreviewRecognizer(folder)
            second.catalog = 'v1'
            second(1, 1000, 1060)
            self.assertEqual(guess.call_count, 1)
            second.catalog = 'v2'
            second(1, 1000, 1060)
            self.assertEqual(guess.call_count, 2)
            Path(folder, '1_1000.json').write_text('broken')
            second(1, 1000, 1060)
            self.assertEqual(guess.call_count, 3)

    def test_source_revision_invalidates_memory_and_filtered_results(self):
        recognize = Mock(return_value=[])
        revision = Mock(return_value='v1')
        cache = SongPreviewCache(recognize, revision=revision)
        cache.get(1, 1000, 1300, .95, now=2000)
        cache.get(1, 1000, 1300, .98, now=2000)
        recognize.assert_called_once()
        revision.return_value = 'v2'
        cache.get(1, 1000, 1300, .95, now=2000)
        self.assertEqual(recognize.call_count, 2)
