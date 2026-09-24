from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.db.live_list import get_live_list_by_tag


class LiveListTest(unittest.TestCase):
    def test_tag_lookup_is_exact_case_insensitive_and_sorted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "live_list.db"
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "CREATE TABLE global_live_list (room_id INTEGER PRIMARY KEY, uname TEXT, adder_uid INTEGER, tags TEXT)"
                )
                conn.executemany(
                    "INSERT INTO global_live_list VALUES (?, ?, ?, ?)",
                    [
                        (1, "Zeta", 1, '["VR", "拼好团"]'),
                        (2, "alpha", 1, '["vr"]'),
                        (3, "Other", 1, '["vr圈"]'),
                        (4, "Broken", 1, "not-json"),
                    ],
                )

            rooms = get_live_list_by_tag("Vr", db_path)
            self.assertEqual([(room["uname"], room["room_id"]) for room in rooms], [("alpha", 2), ("Zeta", 1)])
            self.assertEqual(get_live_list_by_tag("", db_path), [])


if __name__ == "__main__":
    unittest.main()
