from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

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

    def test_backfill_then_trigger_keeps_index_complete(self) -> None:
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
                (200,),
            )


if __name__ == "__main__":
    unittest.main()
