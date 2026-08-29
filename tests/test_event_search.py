from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.db.event_search import (
    get_event_search_status,
    index_pending_events,
    list_recent_events_by_content_indexed,
)


class EventSearchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.db_path = root / "source.db"
        self.search_db_path = root / "search.db"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
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
                )
                """
            )
            conn.executemany(
                "INSERT INTO event VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?)",
                [
                    (1, 1, "DANMU_MSG", 11, "甲", "晚安宝宝", 100),
                    (2, 1, "DANMU_MSG", 12, "乙", "饕餮", 300),
                    (3, 2, "DANMU_MSG", 13, "丙", "饕餮", 400),
                    (4, 1, "DANMU_MSG", 14, "丁", "abxxbc", 500),
                    (5, 1, "DANMU_MSG", None, "无", "饕餮", 600),
                ],
            )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _search(self, keyword: str, **kwargs):
        return list_recent_events_by_content_indexed(
            1,
            keyword,
            db_path=self.db_path,
            search_db_path=self.search_db_path,
            **kwargs,
        )

    def test_exact_room_search_and_false_candidate_filter(self) -> None:
        result = index_pending_events(
            self.db_path,
            self.search_db_path,
            batch_size=2,
            max_batches=None,
        )
        self.assertEqual(result["indexed_documents"], 4)
        self.assertEqual([event["id"] for event in self._search("饕餮")], [2])
        self.assertEqual(self._search("abc"), [])
        self.assertEqual([event["id"] for event in self._search("晚安")], [1])

    def test_unindexed_tail_is_merged_in_timestamp_order(self) -> None:
        index_pending_events(
            self.db_path,
            self.search_db_path,
            batch_size=10,
            max_batches=None,
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO event
                VALUES (6, 1, 'DANMU_MSG', 15, '尾部', '饕餮尾部',
                        NULL, NULL, NULL, NULL, 700)
                """
            )
        self.assertEqual(
            [event["id"] for event in self._search("饕餮")],
            [2, 6],
        )
        status = get_event_search_status(self.db_path, self.search_db_path)
        self.assertTrue(status["available"])
        self.assertEqual(status["last_indexed_event_id"], 5)

    def test_date_and_hidden_range_filters(self) -> None:
        index_pending_events(
            self.db_path,
            self.search_db_path,
            batch_size=10,
            max_batches=None,
        )
        self.assertEqual(self._search("饕餮", end_ts=299), [])
        self.assertEqual(
            self._search("饕餮", excluded_range=(200, 350)),
            [],
        )


if __name__ == "__main__":
    unittest.main()
