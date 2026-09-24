import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.db.clip_marker import add_clip_marker, list_clip_markers, format_clip_marker


class ClipMarkerTest(unittest.TestCase):
    def test_timeline_across_midnight(self):
        zone = ZoneInfo("Asia/Shanghai")
        start = int(datetime(2026, 9, 15, 23, 0, tzinfo=zone).timestamp())
        timestamp = start + 3661
        self.assertEqual(format_clip_marker(timestamp, start, "高光\n时刻"),
                         "[00:01:01] (01:01:01) 高光 时刻")
        self.assertIn("(25:00:00)", format_clip_marker(start + 90000, start, ""))

    def test_order_scope_and_duplicate_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "test.db"
            add_clip_marker(1, 100, 130, 10, 20, 1, "后", db)
            add_clip_marker(1, 100, 120, 11, 21, 2, "先", db)
            add_clip_marker(1, 100, 130, 10, 20, 1, "重复", db)
            add_clip_marker(2, 100, 140, 10, 20, 3, "其他房间", db)
            add_clip_marker(1, 200, 210, 10, 20, 4, "其他场次", db)
            self.assertEqual(list_clip_markers(1, 100, db), [
                {"timestamp": 120, "note": "先"}, {"timestamp": 130, "note": "后"}])
            with self.assertRaises(ValueError):
                add_clip_marker(1, 100, 99, 10, 20, 5, "过早", db)


if __name__ == "__main__":
    unittest.main()
