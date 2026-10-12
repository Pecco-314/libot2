import tempfile
import time
import unittest
from pathlib import Path

from src.db import live_notice
from src.db.event import get_latest_live_cmd, get_latest_live_event_id, list_live_events_after
from src.db.live_recovery import recover_live_start
from src.db.sqlite import connect_sqlite


class LiveRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'test.db'
        self.now = int(time.time())
        with connect_sqlite(self.path) as conn:
            conn.executescript('''
                CREATE TABLE event(id INTEGER PRIMARY KEY,cmd TEXT,room_id INTEGER,title TEXT,timestamp INTEGER);
                CREATE INDEX idx_event_cmd_time ON event(cmd,timestamp);
                CREATE TABLE subscription(group_id INTEGER PRIMARY KEY,room_id INTEGER);
                INSERT INTO subscription VALUES(1,100),(2,100);
                CREATE TABLE state(key TEXT PRIMARY KEY,value TEXT,updated_at TEXT);
            ''')
        live_notice.init_live_notice_db(self.path)

    def tearDown(self):
        self.directory.cleanup()

    def recover(self, **kwargs):
        return recover_live_start(100, self.now-300, db_path=self.path, **kwargs)

    def test_recovery_is_idempotent_and_keeps_source(self):
        event_id = self.recover()
        self.assertIsNotNone(event_id)
        self.assertIsNone(self.recover())
        self.assertEqual(get_latest_live_cmd(100, self.path), 'LIVE')
        self.assertEqual(get_latest_live_event_id([100], self.path), event_id)
        self.assertEqual([r['id'] for r in list_live_events_after([100], 0, self.path)], [event_id])
        with connect_sqlite(self.path) as conn:
            self.assertEqual(conn.execute('SELECT source FROM live_event_recovery').fetchone()[0],
                             'bilibili_room_info')

    def test_suppression_precedes_watcher_and_never_becomes_pending(self):
        event_id = self.recover(suppress_notice=True)
        rows = list_live_events_after([100], 0, self.path)
        live_notice.stage_live_notices(rows, event_id, self.path)
        self.assertEqual(live_notice.list_pending_live_notices(self.path), [])
        with connect_sqlite(self.path) as conn:
            self.assertEqual(conn.execute('SELECT status FROM live_notice_delivery').fetchall(),
                             [('suppressed',), ('suppressed',)])

    def test_native_marker_or_finished_session_prevents_duplicate(self):
        for cmd in ('LIVE', 'PREPARING'):
            with self.subTest(cmd=cmd), connect_sqlite(self.path) as conn:
                conn.execute('DELETE FROM event')
                conn.execute('INSERT INTO event(cmd,room_id,timestamp) VALUES(?,?,?)',
                             (cmd, 100, self.now-305 if cmd=='LIVE' else self.now-1))
            self.assertIsNone(self.recover())

    def test_recovery_for_other_room_does_not_block(self):
        with connect_sqlite(self.path) as conn:
            conn.execute("INSERT INTO event(cmd,room_id,timestamp) VALUES('LIVE',200,?)", (self.now,))
        self.assertIsNotNone(self.recover())

    def test_invalid_time_is_rejected(self):
        with self.assertRaises(ValueError):
            recover_live_start(100, self.now+60, db_path=self.path)

    def test_indexed_polling_ignores_danmaku_and_filters_rooms(self):
        with connect_sqlite(self.path) as conn:
            conn.executemany('INSERT INTO event VALUES(?,?,?,?,?)', [
                (1, 'LIVE', 100, '', 20), (2, 'LIVE', 200, '', 10),
                (3, 'DANMU_MSG', 100, '', 30), (4, 'PREPARING', 100, '', 40),
            ])
        self.assertEqual([r['id'] for r in list_live_events_after([100], 1, self.path)], [4])
        self.assertEqual(get_latest_live_cmd(100, self.path), 'PREPARING')
        self.assertEqual(get_latest_live_event_id([100], self.path), 4)
        self.assertEqual(list_live_events_after([], 0, self.path), [])
        self.assertEqual(get_latest_live_event_id([], self.path), 0)
