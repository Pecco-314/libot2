import argparse
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import backfill_mit3uri_replay_danmaku as replay


def row(**kwargs):
    defaults = dict(
        session_id="test-session", bvid="BVtest", cid=1, dm_id="1",
        progress_ms=1000, session_progress_ms=1000, mid_hash="abcd",
        content="测试直播弹幕", ctime=2000, uid=123, dm_from=3, attr=2,
    )
    defaults.update(kwargs)
    return replay.DmRow(**defaults)


def varint(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def integer(field, value):
    return varint(field << 3) + varint(value)


def blob(field, value):
    return varint((field << 3) | 2) + varint(len(value)) + value


class ReplaySourceFilterTest(unittest.TestCase):
    def test_parser_preserves_source_and_attributes(self):
        encoded = (integer(1, 100) + integer(2, 1200) + blob(6, b"abcd")
                   + blob(7, "测试弹幕".encode()) + integer(8, 3000)
                   + integer(13, 1048576) + integer(27, 1))
        parsed = replay.parse_dm_segment(
            blob(1, encoded), session_id="s", bvid="bv", cid=1,
            session_offset_ms=0, uid_by_hash={"abcd": 123},
        )[0]
        self.assertEqual((parsed.dm_from, parsed.attr, parsed.ctime), (1, 1048576, 3000))

    def test_late_live_batch_is_retained(self):
        self.assertIsNone(replay.replay_row_exclusion(row(ctime=1000+7*86400), 1000))

    def test_video_comments_are_excluded_even_near_publication(self):
        for ctime in (1001, 1500, 1000+365*86400):
            self.assertEqual(replay.replay_row_exclusion(row(dm_from=1, ctime=ctime), 1000), "video_comment")

    def test_unknown_origins_wait_for_review(self):
        self.assertEqual(replay.replay_row_exclusion(row(dm_from=0), 1000), "late_unverified_source")
        self.assertEqual(replay.replay_row_exclusion(row(dm_from=0,ctime=900), 1000), "unverified_source")
        self.assertEqual(replay.replay_row_exclusion(row(dm_from=99), 0), "unverified_source")

    def test_old_audit_cannot_resume_without_filter(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/"audit.db"
            with sqlite3.connect(p) as conn:
                conn.executescript("CREATE TABLE source_row(id INTEGER); CREATE TABLE session_alignment(id INTEGER);")
            with self.assertRaisesRegex(RuntimeError, "source filtering"):
                replay.create_audit_db(p, resume=True)

    def test_audit_stages_exclusions_and_does_not_use_them_as_anchors(self):
        with tempfile.TemporaryDirectory() as d:
            work=Path(d); db=work/"production.db"
            with sqlite3.connect(db) as conn:
                conn.execute("CREATE TABLE event(id INTEGER)")
            metadata=dict(cutoff_timestamp_exclusive=10000,recordings=[dict(
                session_id="test-session", bvid="BVtest", live_date="1970-01-01",
                recording_index=0, start_timestamp=1000, start_time_precision="second",
                session_duration_seconds=60, pubdate=2000,
                pages=[dict(session_offset_ms=0,duration_seconds=60)],
            )])
            (work/"metadata.json").write_text(json.dumps(metadata))
            rows=[row(), row(dm_id="2",dm_from=1), row(dm_id="3",dm_from=0,ctime=9000)]
            with (patch.object(replay,"build_uid_hash_map",return_value={}),
                  patch.object(replay,"load_session_dm",return_value=rows),
                  patch.object(replay,"load_existing_session_rows",return_value=[]),
                  patch.object(replay,"resolve_uname",return_value=None),
                  patch.object(replay,"alignment_candidates",wraps=replay.alignment_candidates) as anchors,
                  contextlib.redirect_stdout(io.StringIO())):
                replay.command_audit(argparse.Namespace(db=db,work_dir=work,duplicate_tolerance_ms=5000))
                self.assertEqual(anchors.call_args.args[0], [rows[0]])
            with sqlite3.connect(work/"audit.db") as conn:
                results=conn.execute("SELECT dm_id,dm_from,dm_attr,action FROM source_row ORDER BY dm_id").fetchall()
            self.assertEqual(results,[("1",3,2,"new"),("2",1,2,"video_comment"),("3",0,2,"late_unverified_source")])
            report=json.loads((work/"audit.json").read_text())
            self.assertEqual(report["actions"]["new"],1)
            self.assertEqual(report["actions"]["video_comment"],1)


if __name__ == "__main__":
    unittest.main()
