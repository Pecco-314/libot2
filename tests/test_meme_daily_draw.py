from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.db.meme import (
    MemePoolExhausted,
    add_meme,
    add_meme_alias,
    get_daily_meme,
    init_meme_db,
    list_meme_title_rank,
    resolve_daily_meme_name,
)


class MemeDailyDrawTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "meme.db"
        init_meme_db(self.db_path)
        add_meme("老婆", "测试内容一", 1, self.db_path)
        add_meme("老婆", "测试内容二", 1, self.db_path)
        add_meme_alias("老婆", "抽老婆", self.db_path)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE meme_entry SET status = 'approved'")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_alias_uses_one_shared_non_repeating_daily_pool(self) -> None:
        canonical_name = resolve_daily_meme_name("抽老婆", self.db_path)
        self.assertEqual(canonical_name, "老婆")

        timezone = ZoneInfo("Asia/Shanghai")
        first = get_daily_meme(
            canonical_name,
            123,
            now=datetime(2026, 8, 30, 12, tzinfo=timezone),
            db_path=self.db_path,
        )
        same_day = get_daily_meme(
            "老婆",
            123,
            now=datetime(2026, 8, 30, 23, tzinfo=timezone),
            db_path=self.db_path,
        )
        second = get_daily_meme(
            "老婆",
            456,
            now=datetime(2026, 8, 30, 13, tzinfo=timezone),
            db_path=self.db_path,
        )

        self.assertEqual(first, same_day)
        self.assertNotEqual(first["id"], second["id"])
        with self.assertRaises(MemePoolExhausted):
            get_daily_meme(
                "老婆",
                789,
                now=datetime(2026, 8, 30, 14, tzinfo=timezone),
                db_path=self.db_path,
            )

    def test_pool_resets_on_next_beijing_date(self) -> None:
        timezone = ZoneInfo("Asia/Shanghai")
        for user_uid in (1, 2):
            get_daily_meme(
                "老婆",
                user_uid,
                now=datetime(2026, 8, 30, 12, tzinfo=timezone),
                db_path=self.db_path,
            )
        self.assertIsNotNone(
            get_daily_meme(
                "老婆",
                3,
                now=datetime(2026, 8, 31, 0, tzinfo=timezone),
                db_path=self.db_path,
            )
        )

    def test_title_rank_counts_only_approved_entries(self) -> None:
        add_meme("其他", "审核通过", 1, self.db_path)
        add_meme("其他", "仍在审核", 1, self.db_path)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE meme_entry SET status = 'approved' WHERE content = ?",
                ("审核通过",),
            )

        self.assertEqual(
            list_meme_title_rank(10, self.db_path),
            [
                {"name": "老婆", "count": 2},
                {"name": "其他", "count": 1},
            ],
        )


if __name__ == "__main__":
    unittest.main()
