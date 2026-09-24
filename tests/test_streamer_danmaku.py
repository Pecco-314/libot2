from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.db.event import get_max_event_id, list_streamer_danmaku_events_after


class StreamerDanmakuTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "event.db"
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE event (
                    id INTEGER PRIMARY KEY,
                    room_id INTEGER NOT NULL,
                    cmd TEXT NOT NULL,
                    uid INTEGER,
                    uname TEXT,
                    content TEXT,
                    timestamp INTEGER
                );
                CREATE TABLE liver (
                    room_id INTEGER PRIMARY KEY,
                    uid INTEGER,
                    uname TEXT,
                    nickname TEXT
                );
                CREATE TABLE subscription (
                    group_id INTEGER PRIMARY KEY,
                    room_id INTEGER NOT NULL
                );
                INSERT INTO liver VALUES (100, 10, '主播原名', '主播昵称');
                INSERT INTO liver VALUES (200, 20, '未订阅主播', NULL);
                INSERT INTO subscription VALUES (123, 100);
                INSERT INTO event VALUES (1, 100, 'DANMU_MSG', 11, '观众', '普通弹幕', 101);
                INSERT INTO event VALUES (2, 100, 'DANMU_MSG', 10, '主播原名', '主播弹幕', 102);
                INSERT INTO event VALUES (3, 100, 'SEND_GIFT', 10, '主播原名', NULL, 103);
                INSERT INTO event VALUES (4, 200, 'DANMU_MSG', 20, '未订阅主播', '不应转发', 104);
                INSERT INTO event VALUES (5, 100, 'DANMU_MSG', 10, '主播原名', '后一条', 105);
                """
            )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_lists_only_own_danmaku_in_subscribed_rooms(self) -> None:
        rows = list_streamer_danmaku_events_after(0, 4, self.db_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], 2)
        self.assertEqual(rows[0]["uname"], "主播昵称")
        self.assertEqual(rows[0]["content"], "主播弹幕")

    def test_honors_event_watermark(self) -> None:
        rows = list_streamer_danmaku_events_after(2, 5, self.db_path)
        self.assertEqual([row["id"] for row in rows], [5])
        self.assertEqual(get_max_event_id(self.db_path), 5)


if __name__ == "__main__":
    unittest.main()
