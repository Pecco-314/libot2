import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.db.meme import init_meme_db, add_meme_image, add_meme_image_tags, add_meme_alias, add_meme_tags


class ImageTagsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'meme.db'
        init_meme_db(self.path)

    def test_independent_pending_entries_and_duplicate_skip(self):
        self.assertEqual(add_meme_image_tags(['xyw', '考子', 'XYW'], 'a.png', 123, self.path), ['xyw', '考子'])
        with sqlite3.connect(self.path) as conn:
            rows = conn.execute('SELECT meme_group_id,content,adder_uid,entry_type,status FROM meme_entry').fetchall()
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0][0], rows[1][0])
        self.assertTrue(all(r[1:] == ('a.png', 123, 'image', 'pending') for r in rows))
        self.assertEqual(add_meme_image_tags(['xyw', '新标签'], 'a.png', 456, self.path), ['新标签'])
        self.assertFalse(add_meme_image('xyw', 'a.png', 123, self.path))

    def test_aliases_share_one_entry(self):
        add_meme_alias('xyw', '考子', self.path)
        self.assertEqual(add_meme_image_tags(['xyw', '考子'], 'a.png', 123, self.path), ['xyw'])

    def test_text_tags_pending_and_alias_deduplication(self):
        add_meme_alias('xyw', '小呜', self.path)
        self.assertEqual(add_meme_tags(['xyw','小呜','考子'], ' 正文 ', 123, self.path), ['xyw','考子'])
        self.assertEqual(add_meme_tags(['xyw','考子'], '正文', 456, self.path), [])
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT content,adder_uid,entry_type,status FROM meme_entry').fetchall(),
                             [('正文',123,'text','pending')]*2)

    def test_invalid_batch_writes_nothing(self):
        for names in ([], ['valid', 'not valid'], ['a' * 33]):
            with self.assertRaises(ValueError):
                add_meme_image_tags(names, 'a.png', 123, self.path)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM meme_group').fetchone()[0], 0)

    def test_transaction_rolls_back_on_failure(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TRIGGER reject_second BEFORE INSERT ON meme_entry WHEN (SELECT COUNT(*) FROM meme_entry)>0 BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            add_meme_image_tags(['xyw', '考子'], 'a.png', 123, self.path)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM meme_entry').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM meme_group').fetchone()[0], 0)
