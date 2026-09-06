from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.db.event import get_latest_uid_by_uname


class EventNameLookupTest(unittest.TestCase):
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
                    gift_name TEXT,
                    gift_num INTEGER,
                    total_coin INTEGER,
                    title TEXT,
                    timestamp INTEGER
                );
                CREATE INDEX idx_uid ON event(uid);
                CREATE INDEX idx_event_room_time ON event(room_id, timestamp);
                CREATE TABLE name_history (
                    uid INTEGER,
                    uname TEXT,
                    first_seen INTEGER,
                    UNIQUE(uid, uname)
                );
                """
            )
            conn.executemany(
                """
                INSERT INTO event(id, room_id, cmd, uid, uname, content, timestamp)
                VALUES (?, ?, 'DANMU_MSG', ?, ?, 'test', ?)
                """,
                [
                    (1, 1, 11, "已索引", 100),
                    (2, 1, 21, "遗漏名字", 200),
                    (3, 1, 22, "遗漏名字", 300),
                    (4, 2, 23, "遗漏名字", 400),
                ],
            )
            conn.execute(
                "INSERT INTO name_history(uid, uname, first_seen) VALUES (11, '已索引', 100)"
            )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_uses_name_history_fast_path(self) -> None:
        self.assertEqual(get_latest_uid_by_uname(1, "已索引", self.db_path), 11)

    def test_falls_back_to_latest_matching_event_in_room(self) -> None:
        self.assertEqual(get_latest_uid_by_uname(1, "遗漏名字", self.db_path), 22)

    def test_fallback_is_case_insensitive(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO event(id, room_id, cmd, uid, uname, content, timestamp)
                VALUES (5, 1, 'DANMU_MSG', 31, 'MixedName', 'test', 500)
                """
            )
        self.assertEqual(get_latest_uid_by_uname(1, "mixedname", self.db_path), 31)

    def test_returns_none_when_both_routes_miss(self) -> None:
        self.assertIsNone(get_latest_uid_by_uname(1, "不存在", self.db_path))

    def test_complete_index_miss_does_not_scan_event(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DROP INDEX idx_event_room_time")
        with patch(
            "src.db.event.is_name_history_index_complete",
            return_value=True,
        ):
            self.assertIsNone(get_latest_uid_by_uname(1, "不存在", self.db_path))


if __name__ == "__main__":
    unittest.main()
