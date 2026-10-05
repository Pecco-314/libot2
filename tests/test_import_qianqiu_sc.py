import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts.import_qianqiu_sc import normalize, reconcile, insert_missing, load_existing, months_between

ROOM = 1967216004


def source(message="你好\n三理", timestamp="2025-11-24 18:00:00", uid=10, price=30):
    return normalize({"room_id": ROOM, "month": "202511", "list": [{
        "uid": uid, "uname": "群友", "price": price, "message": message, "send_time": timestamp,
    }]}, ROOM, "2025-11")[0]


def existing(row, delta=0, eid=1, **overrides):
    return {**row, "id": eid, "timestamp": row["timestamp"]+delta, **overrides}


class SuperchatImportTests(unittest.TestCase):
    def test_normalize_yuan_text_and_beijing_time(self):
        row = source()
        self.assertEqual(row["price"], 30)
        self.assertEqual(row["content"], "你好 三理")
        self.assertEqual(row["timestamp"], 1763978400)

    def test_exact_and_name_changes(self):
        row = source()
        result = reconcile([row], [existing(row, uname="曾用名")])
        self.assertEqual(len(result["matched"]), 1)
        self.assertEqual(result["missing"], [])

    def test_source_duplicates(self):
        row = source()
        result = reconcile([row, row], [])
        self.assertEqual(len(result["missing"]), 1)
        self.assertEqual(result["months"]["2025-11"]["source_duplicates"], 1)

    def test_exact_matches_reserved_before_nearby(self):
        first = source()
        second = source(timestamp="2025-11-24 18:00:02")
        result = reconcile([first, second], [existing(first, 1, 1), existing(second, 0, 2)])
        self.assertEqual(len(result["matched"]), 2)
        self.assertEqual(result["conflicts"], [])

    def test_delayed_unique_and_different_day(self):
        row = source()
        self.assertEqual(reconcile([row], [existing(row, 212)])["months"]["2025-11"]["delayed_unique"], 1)
        self.assertEqual(len(reconcile([row], [existing(row, 86400)])["missing"]), 1)

    def test_delayed_multiple_occurrences_need_review(self):
        a = source()
        b = source(timestamp="2025-11-24 18:02:00")
        result = reconcile([a, b], [existing(a, 80)])
        self.assertEqual(len(result["conflicts"]), 2)
        self.assertEqual(result["missing"], [])

    def test_price_or_content_conflict_not_overwritten(self):
        row = source()
        for original in [existing(row, price=50), existing(row, content="其他留言")]:
            result = reconcile([row], [original])
            self.assertEqual(len(result["conflicts"]), 1)
            self.assertEqual(result["missing"], [])

    def test_room_month_and_uid_validation(self):
        with self.assertRaises(ValueError):
            source(uid=0)
        with self.assertRaises(ValueError):
            source(timestamp="2025-12-01 00:00:00")
        with self.assertRaises(ValueError):
            source(price="30.5")
        self.assertEqual(list(months_between("2025-11", "2026-01")), ["2025-11","2025-12","2026-01"])

    def test_apply_idempotent_and_audited(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.db"
            manifest = Path(directory) / "manifest.json"
            with sqlite3.connect(path) as conn:
                conn.executescript("""CREATE TABLE event(id INTEGER PRIMARY KEY,room_id INTEGER,cmd TEXT,uid INTEGER,
                    uname TEXT,content TEXT,gift_name TEXT,gift_num INTEGER,total_coin INTEGER,title TEXT,timestamp INTEGER);
                    CREATE UNIQUE INDEX idx_event_sc_unique ON event(room_id,uid,content,total_coin,timestamp)
                    WHERE cmd='SUPER_CHAT_MESSAGE';
                    CREATE TABLE name_history(uid INTEGER,uname TEXT,first_seen INTEGER,UNIQUE(uid,uname));""")
            row = source()
            with patch("scripts.import_qianqiu_sc.time.sleep"):
                result = insert_missing(path, [row], manifest, ROOM)
                self.assertEqual(len(result["inserted"]), 1)
                repeated = insert_missing(path, [row], Path(directory)/"repeat.json", ROOM)
                self.assertEqual(repeated["inserted"], [])
            with sqlite3.connect(path) as conn:
                self.assertEqual(conn.execute("SELECT total_coin,gift_name FROM event").fetchone(), (30,"醒目留言"))
                self.assertEqual(conn.execute("SELECT uid,uname FROM name_history").fetchone(), (10,"群友"))
                self.assertEqual(len(load_existing(conn,ROOM,row["timestamp"],row["timestamp"]+1)), 1)
            self.assertTrue(json.loads(manifest.read_text())["complete"])


if __name__ == "__main__":
    unittest.main()
