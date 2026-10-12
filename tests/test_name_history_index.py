from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.db.event import (
    _merge_and_sort_histories,
    list_name_history_by_name,
    list_name_history_by_uid,
)
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

    def test_history_ignores_missing_names_in_all_sources(self):
        init_name_history_index(self.db_path)
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                "INSERT INTO event(id, room_id, cmd, uid, uname, timestamp) "
                "VALUES (?, 1, 'SEND_GIFT', 11, ?, ?)",
                [(2, None, 110), (3, '', 120), (4, '  ', 130)],
            )
            conn.executemany(
                "INSERT INTO name_history VALUES (11, ?, ?)",
                [(None, 10), ('', 20), ('  ', 30), ('索引名字', 250)],
            )
            conn.execute(
                "CREATE TABLE fan_club_name_history "
                "(member_uid INTEGER, uname TEXT, first_seen INTEGER)"
            )
            conn.executemany(
                "INSERT INTO fan_club_name_history VALUES (11, ?, ?)",
                [(None, 30), ('', 40), ('  ', 50), ('粉丝团名字', 200)],
            )
        expected = [{"uid": 11, "history": ["旧名字", "粉丝团名字", "索引名字"]}]
        self.assertEqual(self.lookup("旧名字"), expected)
        with patch("src.db.event.connect_sqlite", lambda: connect_sqlite(self.db_path)):
            self.assertEqual(list_name_history_by_uid(11), expected)

    def test_missing_names_produce_no_user_history(self):
        self.assertEqual(_merge_and_sort_histories(
            [(11, None, 1), (11, '', 2), (11, '  ', 3), (None, '名字', 4)], []
        ), [])

    def test_valid_names_keep_spelling_and_first_seen_order(self):
        self.assertEqual(_merge_and_sort_histories(
            [(11, 'Mito_Mashiro', 100), (11, '新名', 90)],
            [(11, 'Mito_Mashiro', 80), (11, '新名', 200)],
        ), [{"uid": 11, "history": ['Mito_Mashiro', '新名']}])

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

    def test_case_insensitive_lookup_preserves_spelling_in_all_paths(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE event SET uname='Mito_Mashiro'")
        expected = [{"uid": 11, "history": ["Mito_Mashiro"]}]
        self.assertEqual(self.lookup("mito_mashiro"), expected)
        init_name_history_index(self.db_path)
        self.assertEqual(self.lookup("MITO_MASHIRO"), expected)
        backfill(self.db_path, batch_size=1)
        self.assertEqual(self.lookup("mito_mashiro"), expected)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO event(id, room_id, cmd, uid, uname, timestamp) "
                "VALUES (2, 1, 'DANMU_MSG', 22, 'MITO_MASHIRO', 200)"
            )
            plan = conn.execute(
                "EXPLAIN QUERY PLAN SELECT uid FROM name_history "
                "WHERE uname = ? COLLATE NOCASE", ("mito_mashiro",)
            ).fetchall()
        self.assertTrue(any("SEARCH" in r[3] and "idx_nh_name_uid_nocase" in r[3] for r in plan))
        self.assertEqual(self.lookup("mito_mashiro"), expected + [
            {"uid": 22, "history": ["MITO_MASHIRO"]}
        ])

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
