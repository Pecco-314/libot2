from pathlib import Path
import sqlite3
import tempfile
import unittest

from src.common.singers import split_singers
from src.db.singer_alias import init_singer_alias_db, add_singer_alias, resolve_singer_alias
from src.db.song_list import init_song_list_db, list_songs_by_singer, list_recent_song_records_by_singer


class SingerAliasTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "songs.db"
        init_song_list_db(self.db)
        init_singer_alias_db(self.db)
        with sqlite3.connect(self.db) as conn:
            conn.executemany("INSERT INTO song_info(id,title,original_singer) VALUES(?,?,?)", [
                (1, "泡沫", "G.E.M. 邓紫棋"), (2, "优尼瓦思电力", "三理Mit3uri"),
                (3, "合唱", "七海Nana7mi/三理Mit3uri/阿萨Aza"), (4, "POP/STARS", "K/DA"),
                (5, "Golden", "HUNTR/X/七海Nana7mi"),
            ])
            conn.executemany("INSERT INTO song_record(song_id,record_date) VALUES(?,?)",
                             [(3, f"2026-09-{i:02}") for i in range(1,8)]+[(2,"2026-09-08")])

    def test_alias_matches_exact_artist_and_chorus(self):
        self.assertEqual([r["id"] for r in list_songs_by_singer("邓紫棋",self.db)], [1])
        self.assertEqual(list_songs_by_singer("三理",self.db), list_songs_by_singer("三理Mit3uri",self.db))
        self.assertEqual([r["id"] for r in list_songs_by_singer("七海",self.db)], [3,5])
        self.assertEqual(list_songs_by_singer("海",self.db), [])

    def test_recent_records_share_alias_matching_and_order(self):
        rows = list_recent_song_records_by_singer("三理",db_path=self.db)
        self.assertEqual(rows, list_recent_song_records_by_singer("三理Mit3uri",db_path=self.db))
        self.assertEqual(len(rows),5)
        self.assertEqual(rows[0]["record_date"],"2026-09-08")
        self.assertEqual(rows[1]["record_date"],"2026-09-07")

    def test_register_casefold_alias_idempotently(self):
        self.assertEqual(add_singer_alias("GEM", "G.E.M. 邓紫棋",self.db), ("G.E.M. 邓紫棋",True))
        self.assertEqual(add_singer_alias("gem", "g.e.m. 邓紫棋",self.db), ("G.E.M. 邓紫棋",False))
        self.assertEqual(resolve_singer_alias(" GEM ",db_path=self.db), "G.E.M. 邓紫棋")
        self.assertEqual([r["id"] for r in list_songs_by_singer("gem",self.db)], [1])
        init_singer_alias_db(self.db)
        self.assertEqual(resolve_singer_alias("GEM",db_path=self.db), "G.E.M. 邓紫棋")

    def test_ambiguous_target_and_conflicts_rejected(self):
        for alias, target in [("三理", "七海Nana7mi"), ("三理Mit3uri", "七海Nana7mi"),
                              ("测试", "无此歌手"), ("空", ""), ("测试", "三理")]:
            with self.assertRaises(ValueError):
                add_singer_alias(alias,target,self.db)

    def test_slashes_in_band_names(self):
        self.assertEqual(split_singers("K/DA/阿萨Aza/HUNTR/X"), ["K/DA","阿萨Aza","HUNTR/X"])
        self.assertEqual([r["id"] for r in list_songs_by_singer("k/da",self.db)], [4])
        self.assertEqual(add_singer_alias("女团", "HUNTR/X",self.db), ("HUNTR/X",True))
        self.assertEqual([r["id"] for r in list_songs_by_singer("女团",self.db)], [5])

    def test_default_aliases_work_before_schema_initialization(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("DROP TABLE singer_alias")
        self.assertEqual([r["id"] for r in list_songs_by_singer("邓紫棋",self.db)], [1])

    def test_collaboration_separators_and_recent_records(self):
        cases = {
            "Taylor Swift ft. Ed Sheeran & Future": ["Taylor Swift", "Ed Sheeran", "Future"],
            "Ariana Grande & The Weeknd": ["Ariana Grande", "The Weeknd"],
            "Jessie J, Ariana Grande & Nicki Minaj": ["Jessie J", "Ariana Grande", "Nicki Minaj"],
            "Wiz Khalifa feat. Charlie Puth": ["Wiz Khalifa", "Charlie Puth"],
            "Ariana Grande featuring Mac Miller": ["Ariana Grande", "Mac Miller"],
            "DAOKO × 米津玄師": ["DAOKO", "米津玄師"],
            "甲＆乙，丙、丁": ["甲", "乙", "丙", "丁"],
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(split_singers(value), expected)
        with sqlite3.connect(self.db) as conn:
            conn.execute("INSERT INTO song_info(id,title,original_singer) VALUES(6,'Love Me Harder','Ariana Grande & The Weeknd')")
            conn.execute("INSERT INTO song_record(song_id,record_date) VALUES(6,'2026-09-24')")
        self.assertEqual(add_singer_alias("盆栽", "The Weeknd", self.db), ("The Weeknd", True))
        self.assertEqual([r["id"] for r in list_songs_by_singer("盆栽", self.db)], [6])
        self.assertEqual(list_recent_song_records_by_singer("盆栽", db_path=self.db)[0]["song_id"], 6)

    def test_ampersands_in_group_names_are_preserved(self):
        for name in ("Selena Gomez & The Scene", "The Mamas & The Papas"):
            self.assertEqual(split_singers(name), [name])
            self.assertEqual(split_singers(name.lower() + " & 三理Mit3uri"), [name, "三理Mit3uri"])
            self.assertEqual(split_singers(name.replace('&', '＆')), [name])

    def test_member_queries_include_group_and_solo_songs(self):
        group = "Selena Gomez & The Scene"
        with sqlite3.connect(self.db) as conn:
            conn.executemany(
                "INSERT INTO song_info(id,title,original_singer) VALUES(?,?,?)",
                [(6, "Bad Liar", "Selena Gomez"), (7, "Who Says", group),
                 (8, "Hit The Lights", group),
                 (9, "合作", group.lower() + "/Selena Gomez/三理Mit3uri")],
            )
            conn.executemany(
                "INSERT INTO song_record(song_id,record_date) VALUES(?,?)",
                [(6, "2026-09-01"), (7, "2026-09-02"),
                 (8, "2026-09-03"), (8, "2026-09-03"), (9, "2026-09-04")],
            )
        add_singer_alias("赛琳娜", "Selena Gomez", self.db)
        for query in ("Selena Gomez", " SELENA GOMEZ ", "赛琳娜"):
            with self.subTest(query=query):
                songs = list_songs_by_singer(query, self.db)
                self.assertEqual([row["id"] for row in songs], [8, 6, 7, 9])
                self.assertEqual(songs[0]["original_singer"], group)
                recent = list_recent_song_records_by_singer(query, limit=3, db_path=self.db)
                self.assertEqual([row["song_id"] for row in recent], [9, 8, 8])
                self.assertGreater(recent[1]["record_id"], recent[2]["record_id"])
        self.assertEqual(
            [row["id"] for row in list_songs_by_singer(group, self.db)], [8, 7, 9]
        )
        self.assertEqual(
            [row["song_id"] for row in list_recent_song_records_by_singer(group, db_path=self.db)],
            [9, 8, 8, 7],
        )
        self.assertEqual(list_songs_by_singer("The Scene", self.db), [])
        self.assertEqual(list_songs_by_singer("Selena", self.db), [])


if __name__ == "__main__":
    unittest.main()
