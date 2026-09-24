from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.db.event import list_name_history_by_name
from src.db.sqlite import connect_sqlite

from scripts.backfill_name_history_index import backfill
from src.db.name_history import (
    init_name_history_index,
    is_name_history_index_complete,
)


class NameHistoryIndexTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "event.db"
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
            conn.execute(
                """
                INSERT INTO event(id, room_id, cmd, uid, uname, timestamp)
                VALUES (1, 1, 'DANMU_MSG', 11, '旧名字', 100)
                """
            )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def lookup(self, name):
        with patch("src.db.event.connect_sqlite", lambda: connect_sqlite(self.db_path)):
            return list_name_history_by_name(name)

    def test_history_without_index_falls_back_to_events(self):
        self.assertEqual(self.lookup("旧名字"), [{"uid": 11, "history": ["旧名字"]}])
        self.assertEqual(self.lookup("不存在"), [])

    def test_history_incomplete_index_includes_unindexed_users(self):
        init_name_history_index(self.db_path)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO name_history VALUES (22, '旧名字', 200)")
        self.assertEqual([r['uid'] for r in self.lookup("旧名字")], [11, 22])

    def test_history_complete_index_merges_tail_and_same_names(self):
        init_name_history_index(self.db_path)
        backfill(self.db_path, batch_size=1)
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                "INSERT INTO event(id, room_id, cmd, uid, uname, timestamp) "
                "VALUES (?, 1, 'DANMU_MSG', ?, ?, ?)",
                [(2, 11, '虚弱蜜蜂', 200), (3, 22, '旧名字', 300)],
            )
        self.assertEqual(self.lookup("虚弱蜜蜂"),
                         [{"uid": 11, "history": ["旧名字", "虚弱蜜蜂"]}])
        self.assertEqual([r['uid'] for r in self.lookup("旧名字")], [11, 22])
        self.assertEqual(self.lookup("不存在"), [])

    def test_missing_cursor_falls_back(self):
        init_name_history_index(self.db_path)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE name_history_index_meta SET value=1 WHERE key='complete'")
            conn.execute("DELETE FROM name_history_index_meta WHERE key='cursor_event_id'")
        self.assertEqual(self.lookup("旧名字"), [{"uid": 11, "history": ["旧名字"]}])

    def test_daily_sync_and_tail_lookup(self) -> None:
        init_name_history_index(self.db_path)
        backfill(self.db_path, batch_size=1)
        with sqlite3.connect(self.db_path) as conn:
            self.assertTrue(is_name_history_index_complete(conn))
            self.assertEqual(
                conn.execute(
                    "SELECT uid, uname, first_seen FROM name_history"
                ).fetchall(),
                [(11, "旧名字", 100)],
            )
            conn.execute(
                """
                INSERT INTO event(id, room_id, cmd, uid, uname, timestamp)
                VALUES (2, 1, 'DANMU_MSG', 22, '新名字', 200)
                """
            )
            self.assertEqual(
                conn.execute(
                    "SELECT first_seen FROM name_history WHERE uid = 22 AND uname = '新名字'"
                ).fetchone(),
                None,
            )
        from src.db.event import get_latest_uid_by_uname
        self.assertEqual(get_latest_uid_by_uname(1, '新名字', self.db_path), 22)
        self.assertIsNone(get_latest_uid_by_uname(1, '不存在', self.db_path))
        backfill(self.db_path, batch_size=1)
        with sqlite3.connect(self.db_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT first_seen FROM name_history WHERE uid=22"
            ).fetchone(), (200,))
            self.assertEqual(conn.execute(
                "SELECT value FROM name_history_index_meta WHERE key='cursor_event_id'"
            ).fetchone(), (2,))


if __name__ == "__main__":
    unittest.main()
