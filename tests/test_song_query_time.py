from datetime import datetime, timezone
import unittest

from src.common.song_time import BEIJING, parse_song_query_time


class SongQueryTimeTests(unittest.TestCase):
    def test_supported_formats(self):
        now = datetime(2026, 10, 24, 12, 30, 40, tzinfo=BEIJING)
        for value, expected in [
            ("", now),
            (" 08:15 ", now.replace(hour=8, minute=15, second=0)),
            ("08:15:23", now.replace(hour=8, minute=15, second=23)),
            ("2026-10-23 23:59", datetime(2026, 10, 23, 23, 59, tzinfo=BEIJING)),
            ("2026-10-23 23:59:12", datetime(2026, 10, 23, 23, 59, 12, tzinfo=BEIJING)),
        ]:
            with self.subTest(value=value):
                self.assertEqual(parse_song_query_time(value, now=now), int(expected.timestamp()))

    def test_date_uses_beijing_even_near_midnight(self):
        now = datetime(2026, 10, 23, 16, 1, tzinfo=timezone.utc)
        expected = datetime(2026, 10, 24, 23, 50, tzinfo=BEIJING)
        self.assertEqual(parse_song_query_time('23:50', now=now), int(expected.timestamp()))

    def test_invalid(self):
        for value in ('24:00', '12:60', '12:00:60', '2026-02-30 12:00', '昨天', '2026-10-24'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_song_query_time(value)
