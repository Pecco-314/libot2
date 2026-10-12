import json
import sqlite3
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from scripts import backfill_monitor_gap as gap
from scripts.danmaku_spider import _build_event_rows
from dataclasses import asdict


class MonitorGapBackfillTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = Path(self.directory.name) / "live.db"
        self.cold = Path(self.directory.name) / "cold.db"
        with sqlite3.connect(self.db) as conn:
            conn.executescript("""
                CREATE TABLE event (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, room_id INTEGER, cmd TEXT,
                    uid INTEGER, uname TEXT, content TEXT, gift_name TEXT, gift_num INTEGER,
                    total_coin INTEGER, title TEXT, timestamp INTEGER
                );
                CREATE INDEX idx_event_cmd_time ON event(cmd,timestamp);
                CREATE INDEX idx_event_room_time ON event(room_id,timestamp);
                CREATE TABLE live_event_recovery(event_id INTEGER,source TEXT,suppress_notice INTEGER);
                CREATE TABLE live_notice_delivery(event_id INTEGER,group_id INTEGER,status TEXT);
                INSERT INTO event(room_id,cmd,title,timestamp) VALUES(100,'LIVE','test',1000);
                INSERT INTO live_event_recovery VALUES(1,'bilibili_room_info',1);
                INSERT INTO live_notice_delivery VALUES(1,5,'suppressed'),(1,6,'suppressed');
                INSERT INTO event(room_id,cmd,uid,content,timestamp)
                    VALUES(100,'DANMU_MSG',2,'monitor message',1300);
            """)
        # Identical messages, including two at the same second, are distinct source records.
        raw = [{
            "page_offset": 0, "record_index": i + 2,
            "record": {"type": 0, "payloadKind": 1, "actorId": 0,
                       "ts": ts, "payload": {"rawText": "晚安"}},
        } for i, ts in enumerate((1030123, 1030456, 1033000))]
        frame = {"actors": [{"uid": 2, "name": "user"}], "roomEmojis": [], "records": raw}
        rows, _, _ = _build_event_rows({**frame, "records": [r["record"] for r in raw]}, 100)
        self.report = {
            "room_id": 100, "live_start": 1000, "monitor_first_message": 1300,
            "selected_source": {"live_id": "live-a", "raw_frame": frame,
                                "normalized_candidates": [asdict(row) for row in rows]},
        }
        self.evidence = {
            "metadata": {"channel": {"roomId": 100},
                         "live": {"liveId": "live-a", "startDate": 1000000}},
            "opening_records": [{"type": 11, "ts": 1028000, "payload": {"text": "来自 uploader"}}],
        }

    def tearDown(self):
        self.directory.cleanup()

    def run_backfill(self, apply=True):
        return gap.backfill(self.report, self.evidence, apply=apply,
                            db_path=self.db, archive_db=self.cold)

    def test_dry_run_does_not_mutate_either_database(self):
        result = self.run_backfill(False)
        self.assertEqual(result["would_insert_danmaku"], 3)
        self.assertFalse(self.cold.exists())
        with sqlite3.connect(self.db) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='monitor_gap_backfill'").fetchone())
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM event").fetchone()[0], 2)

    def test_repeated_messages_source_precision_and_live_reuse(self):
        first = self.run_backfill()
        self.assertEqual(first["inserted_danmaku"], 3)
        self.assertEqual(first["live_event_id"], 1)
        again = self.run_backfill()
        self.assertEqual(again["inserted_danmaku"], 0)
        self.assertEqual(again["already_imported"], 3)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT timestamp FROM event WHERE cmd='LIVE'").fetchall(), [(1000,)])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM event WHERE content='晚安'").fetchone()[0], 3)
            self.assertEqual(conn.execute("SELECT status FROM live_notice_delivery").fetchall(), [("suppressed",)] * 2)
            self.assertEqual(conn.execute("SELECT source FROM live_event_recovery").fetchone()[0], "bilibili_room_info")
            self.assertEqual(conn.execute("SELECT source_timestamp_ms FROM monitor_gap_backfill WHERE cmd='LIVE'").fetchone()[0], 1000000)
            self.assertEqual(conn.execute("SELECT content FROM event WHERE id=2").fetchone()[0], "monitor message")
        with sqlite3.connect(self.cold) as conn:
            self.assertEqual(conn.execute("SELECT source_timestamp_ms FROM danmaku_source_record ORDER BY id").fetchall(), [(1030123,), (1030456,), (1033000,)])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM danmaku_source_link").fetchone()[0], 3)
            payload = json.loads(zlib.decompress(conn.execute("SELECT payload FROM danmaku_source_artifact LIMIT 1").fetchone()[0]))
            self.assertEqual(payload["live_evidence"], self.evidence)

    def test_conflicting_source_or_unsuppressed_notice_is_rejected(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("INSERT INTO event(room_id,cmd,content,timestamp) VALUES(100,'DANMU_MSG','other source',1050)")
        with self.assertRaisesRegex(ValueError, "其他来源"):
            self.run_backfill()
        self.assertFalse(self.cold.exists())
        with sqlite3.connect(self.db) as conn:
            conn.execute("DELETE FROM event WHERE content='other source'")
            conn.execute("UPDATE live_notice_delivery SET status='pending'")
        with self.assertRaisesRegex(ValueError, "通知"):
            self.run_backfill()

    def test_source_collection_marker_cannot_replace_actual_start(self):
        self.evidence["metadata"]["live"]["startDate"] = 1028000
        with self.assertRaisesRegex(ValueError, "场次信息"):
            self.run_backfill()

    def test_outside_gap_and_tampered_candidates_are_rejected(self):
        self.report["selected_source"]["normalized_candidates"][0]["content"] = "edited"
        with self.assertRaisesRegex(ValueError, "标准化"):
            self.run_backfill()

    def test_crash_after_main_commit_resumes_without_duplicates(self):
        with patch.object(gap, "link_archive", side_effect=RuntimeError("interrupted")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                self.run_backfill()
        result = self.run_backfill()
        self.assertEqual(result["inserted_danmaku"], 0)
        self.assertEqual(result["cold_source_links"], 3)

    def test_event_and_ledger_roll_back_together(self):
        with sqlite3.connect(self.db) as conn:
            conn.executescript("""
                CREATE TRIGGER reject_second_gap BEFORE INSERT ON event
                WHEN new.content='晚安' AND EXISTS(SELECT 1 FROM event WHERE content='晚安')
                BEGIN SELECT RAISE(ABORT,'test failure'); END;
            """)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "test failure"):
            self.run_backfill()
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM event").fetchone()[0], 2)
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='monitor_gap_backfill'").fetchone())
            conn.execute("DROP TRIGGER reject_second_gap")
        self.assertEqual(self.run_backfill()["inserted_danmaku"], 3)

    def test_edited_imported_events_cannot_be_silently_duplicated(self):
        self.run_backfill()
        with sqlite3.connect(self.db) as conn:
            conn.execute("UPDATE event SET content='edited' WHERE content='晚安'")
        with self.assertRaisesRegex(ValueError, "已补录事件"):
            self.run_backfill()


if __name__ == "__main__":
    unittest.main()
