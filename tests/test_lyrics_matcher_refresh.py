import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.capture import guesser
from src.capture.fuzzy import LyricsMatcher, LyricsSearchIndex
from src.db.sqlite import connect_sqlite


class LyricsMatcherRefreshTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "songs.db"
        with connect_sqlite(self.path) as conn:
            conn.executescript("""
                CREATE TABLE song_info (
                    id INTEGER PRIMARY KEY, title TEXT, title_trans TEXT,
                    original_singer TEXT, lyrics_cleaned TEXT, lyrics TEXT,
                    updated_at TEXT DEFAULT '2026-10-02 00:00:00'
                );
                CREATE TABLE song_record (song_id INTEGER, record_date TEXT);
                CREATE TABLE transcript (content TEXT);
                INSERT INTO song_info (id, title, original_singer, lyrics_cleaned)
                VALUES (1, '旧歌', '歌手甲', '森林深处有一座小木屋');
            """)
        self.db_mock = patch(
            "src.capture.fuzzy.connect_sqlite",
            side_effect=lambda: connect_sqlite(self.path),
        ).start()
        self.addCleanup(patch.stopall)
        self.clock = patch("src.capture.fuzzy.time.monotonic", return_value=100.0).start()
        self.matcher = LyricsMatcher()

    def write(self, sql, params=()):
        # 独立连接模拟 spider、管理脚本等外部写入。
        with sqlite3.connect(self.path) as conn:
            conn.execute(sql, params)

    def advance(self):
        self.clock.return_value += 61.0

    def test_new_song_becomes_visible_without_manual_refresh(self):
        self.matcher.search("森林深处")
        self.write(
            "INSERT INTO song_info (id,title,original_singer,lyrics_cleaned) VALUES (2,?,?,?)",
            ("新歌", "歌手乙", "窗外的风带来远方的消息"),
        )
        self.advance()
        self.assertEqual(self.matcher.search("窗外的风带来远方的消息")[0]["id"], 2)

    def test_background_lyrics_fill_after_initial_empty_index(self):
        self.write("UPDATE song_info SET lyrics_cleaned=NULL")
        self.assertEqual(self.matcher.search("森林深处"), [])
        self.write("UPDATE song_info SET lyrics_cleaned='森林深处有一座小木屋'")
        self.advance()
        self.assertEqual(self.matcher.search("森林深处")[0]["score"], 100)

    def test_same_length_edit_without_timestamp_change(self):
        self.matcher.search("森林深处")
        old_index = self.matcher._index
        self.write("UPDATE song_info SET lyrics_cleaned='海洋深处有一座小木屋'")
        self.advance()
        self.assertEqual(self.matcher.search("海洋深处有一座小木屋")[0]["score"], 100)
        self.assertIsNot(self.matcher._index, old_index)

    def test_metadata_edit_is_visible(self):
        self.matcher.search("森林深处")
        self.write("UPDATE song_info SET title='新歌名',original_singer='歌手乙'")
        self.advance()
        result = self.matcher.search("森林深处")[0]
        self.assertEqual((result["title"], result["original_singer"]), ("新歌名", "歌手乙"))

    def test_raw_lyrics_change_refreshes_line_based_search(self):
        self.matcher.search('森林深处')
        self.write("UPDATE song_info SET lyrics='[00:00]作曲：某人\n[00:02]海洋深处有一座小木屋'")
        self.advance()
        self.assertEqual(self.matcher.search('海洋深处有一座小木屋')[0]['score'],100)

    def test_deleted_or_cleared_lyrics_disappear(self):
        for sql in ("DELETE FROM song_info", "UPDATE song_info SET lyrics_cleaned=''"):
            with self.subTest(sql=sql):
                self.write("INSERT OR REPLACE INTO song_info (id,lyrics_cleaned) VALUES (1,'森林深处')")
                self.matcher.refresh()
                self.write(sql)
                self.advance()
                self.assertEqual(self.matcher.search("森林深处"), [])

    def test_checks_are_throttled_and_unrelated_writes_do_not_rebuild(self):
        self.matcher.search("森林深处")
        old_index = self.matcher._index
        for _ in range(20):
            self.matcher.search("森林深处")
        self.assertEqual(self.db_mock.call_count, 1)
        self.write("INSERT INTO transcript VALUES ('一条实时转写')")
        self.advance()
        self.matcher.search("森林深处")
        self.assertEqual(self.db_mock.call_count, 2)
        self.assertIs(self.matcher._index, old_index)

    def test_guess_path_observes_new_lyrics(self):
        with patch.object(guesser, "_matcher", self.matcher):
            guesser._guess_from_texts(["森林深处有一座小木屋"])
            self.write(
                "INSERT INTO song_info (id,title,original_singer,lyrics_cleaned) VALUES (2,?,?,?)",
                ("新歌", "歌手乙", "窗外的风带来远方的消息"),
            )
            self.advance()
            result = guesser._guess_from_texts(["窗外的风带来远方的消息"])
            self.assertEqual(result[0]["title"], "新歌")
            self.assertEqual(result[0]["final_score"], 1.0)

    def test_injected_index_does_not_access_database(self):
        index = LyricsSearchIndex.from_rows([(7, "测试", None, "甲", "[]", 0, "森林深处")])
        matcher = LyricsMatcher(index)
        self.assertEqual(matcher.search("森林深处")[0]["id"], 7)
        self.db_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
