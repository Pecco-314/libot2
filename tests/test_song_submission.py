from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from src.common.song_submission import parse_song_submission
from src.db.song_list import submit_song_list, search_songs_by_title


class SongSubmissionTests(unittest.TestCase):
    def test_fuzzy_search_keeps_case_insensitive_exact_priority(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'songs.db'
            with sqlite3.connect(path) as conn:
                conn.executescript('''CREATE TABLE song_info(id INTEGER PRIMARY KEY,title TEXT,title_trans TEXT,original_singer TEXT);
                CREATE TABLE song_record(id INTEGER PRIMARY KEY,song_id INTEGER,record_date TEXT);
                INSERT INTO song_info VALUES(1,'Say Something','','Singer');
                INSERT INTO song_info VALUES(2,'Say So','说吧','Singer');
                INSERT INTO song_record VALUES(1,1,'2026-09-29');''')
            with patch('src.db.song_list.connect_sqlite', side_effect=lambda: sqlite3.connect(path)):
                for query in ['Say So', 'say so', 'Say so']:
                    for singer in [None, 'sing']:
                        self.assertEqual(search_songs_by_title(query,limit=1,singer=singer)[0]['id'],2)
                self.assertEqual(search_songs_by_title('Say',limit=1)[0]['id'],1)
                self.assertEqual(search_songs_by_title('Something',limit=1)[0]['id'],1)
                self.assertEqual(search_songs_by_title('说',limit=1)[0]['id'],2)

    def test_reject_invalid_lines(self):
        for text in ['1 # song - singer', '[2026-02-30]\n1 # song - singer',
                     '[2026-10-16]\n1 # song - singer\n无关内容',
                     '[2026-10-16]\n? # 未找到', '[2026-10-16]\n1 # ？ - singer',
                     '[2026-10-16]\n1 # song - singer\n\n2 # song - singer',
                     '[2026-10-16]abc\n1 # song - singer']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_song_submission(text)

    def test_atomic_and_duplicate_performances(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'test.db'
            with sqlite3.connect(path) as conn:
                conn.executescript('''CREATE TABLE song_info(id INTEGER PRIMARY KEY,title TEXT,original_singer TEXT);
                CREATE TABLE song_record(id INTEGER PRIMARY KEY,song_id INTEGER,record_date TEXT);
                INSERT INTO song_info VALUES(1,'song','singer');''')
            for ending in ['2 # missing - singer', '1 # unrelated - singer']:
                with self.assertRaises(ValueError):
                    submit_song_list('[2026-10-16]\n1 # song - singer\n'+ending, path)
                with sqlite3.connect(path) as conn:
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM song_record').fetchone()[0],0)
            self.assertEqual(submit_song_list('[2026-10-16]\n1 # song - singer\n1 # song - singer',path),2)
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON song_record WHEN (SELECT COUNT(*) FROM song_record)>=3 BEGIN SELECT RAISE(ABORT,'failed'); END")
            with self.assertRaises(sqlite3.Error):
                submit_song_list('[2026-10-16]\n1 # song - singer\n1 # song - singer',path)
            with sqlite3.connect(path) as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM song_record').fetchone()[0],2)
