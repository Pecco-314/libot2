import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.db.fan_club import MEMBER_UID_INDEX, init_fan_club_db, list_latest_member_medals
from scripts.index_member_medals import build_index


class MemberMedalHistoryTests(unittest.TestCase):
    def test_last_seen_per_room_survives_absence_and_uses_latest_not_max_level(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'test.db'
            init_fan_club_db(path)
            with sqlite3.connect(path) as c:
                c.executemany('INSERT INTO fan_club_target(uid,full_name,short_name,enabled,created_at,updated_at) VALUES(?,?,?,1,0,0)',
                              [(1,'主播一','一'),(2,'主播二','二')])
                for run,day in [(1,'2026-10-01'),(2,'2026-10-02'),(3,'2026-10-03')]:
                    c.execute("INSERT INTO fan_club_run(id,snapshot_date,started_at,status) VALUES(?,?,0,'complete')",(run,day))
                for sid,run,room,status in [(1,1,1,'complete'),(2,2,1,'complete'),(3,3,1,'complete'),(4,1,2,'complete'),(5,3,2,'failed')]:
                    c.execute('INSERT INTO fan_club_snapshot(id,run_id,streamer_uid,snapshot_ts,started_at,finished_at,status) VALUES(?,?,?,0,0,?,?)',(sid,run,room,sid,status))
                c.executemany("INSERT INTO fan_club_member(snapshot_id,member_uid,uname,level,guard_level,user_rank) VALUES(?,?,'用户',?,?,1)",
                              [(1,99,30,3),(2,99,20,0),(3,88,50,1),(4,99,15,3),(5,99,40,1)])
            rows=list_latest_member_medals(99,path)
            self.assertEqual([(r['target_uid'],r['level'],r['guard_level'],r['snapshot_date']) for r in rows],
                             [(1,20,0,'2026-10-02'),(2,15,3,'2026-10-01')])
            self.assertEqual(list_latest_member_medals(123,path),[])
            with sqlite3.connect(path) as c:
                plan = c.execute(f'EXPLAIN QUERY PLAN SELECT level,guard_level FROM fan_club_member INDEXED BY {MEMBER_UID_INDEX} WHERE member_uid=?', (99,)).fetchall()
                self.assertTrue(any('COVERING INDEX' in r[3] for r in plan))
                c.execute(f'DROP INDEX {MEMBER_UID_INDEX}')
            self.assertEqual(list_latest_member_medals(99,path),rows)
            self.assertEqual(list_latest_member_medals(123,path),[])
            # 初始化存量数据库不能触发大索引迁移。
            init_fan_club_db(path)
            with sqlite3.connect(path) as c:
                self.assertIsNone(c.execute('SELECT 1 FROM sqlite_master WHERE name=?', (MEMBER_UID_INDEX,)).fetchone())
            build_index(path)
            self.assertEqual(list_latest_member_medals(99,path),rows)

    def test_interrupted_migration_preserves_members(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'test.db'
            init_fan_club_db(path)
            with sqlite3.connect(path) as c:
                c.execute(f'DROP INDEX {MEMBER_UID_INDEX}')
                c.executemany("INSERT INTO fan_club_member(snapshot_id,member_uid,uname,level,guard_level,user_rank) VALUES(1,?,'用户',20,0,1)", [(n,) for n in range(10000)])
            with self.assertRaises(sqlite3.OperationalError):
                build_index(path, max_seconds=0.000001)
            with sqlite3.connect(path) as c:
                self.assertEqual(c.execute('SELECT COUNT(*) FROM fan_club_member').fetchone()[0],10000)
                self.assertIsNone(c.execute('SELECT 1 FROM sqlite_master WHERE name=?', (MEMBER_UID_INDEX,)).fetchone())
