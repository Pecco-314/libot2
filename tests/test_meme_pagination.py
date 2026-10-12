"""Pagination regressions; all DB writes are confined to a temporary database."""
import ast
import importlib.util
import secrets
import sqlite3
import tempfile
import unittest
from functools import wraps
from pathlib import Path
from unittest.mock import patch

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from src.db import sqlite as sqlite_helpers

ROOT = Path(__file__).resolve().parents[1]


class MemePaginationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'meme.db'
        spec = importlib.util.spec_from_file_location('isolated_meme', ROOT / 'src/db/meme.py')
        self.meme = importlib.util.module_from_spec(spec)
        # 默认连接绑定临时数据库，所有初始化与查询都在隔离环境中执行。
        with patch.object(sqlite_helpers, 'DEFAULT_DB_PATH', self.db):
            spec.loader.exec_module(self.meme)
        self.meme.init_meme_db(self.db)
        with sqlite3.connect(self.db) as con:
            con.execute("INSERT INTO meme_group(id, display_name) VALUES (1, '测试')")
            con.executemany(
                "INSERT INTO meme_entry(meme_group_id, content, adder_uid, status, created_at) "
                "VALUES (1, ?, 1, ?, '2026-09-01 00:00:00')",
                [(f'内容{i}', 'pending' if i < 60 else 'approved' if i < 2270 else 'rejected')
                 for i in range(2307)],
            )

    def test_every_record_accessible_without_2000_cap(self):
        first = self.meme.get_meme_page(status='all')
        self.assertEqual((first['total'], first['pending'], first['pages']), (2307, 60, 47))
        ids = []
        for number in range(1, 48):
            page = self.meme.get_meme_page(page=number, status='all')
            self.assertLessEqual(len(page['items']), 50)
            ids.extend(row['id'] for row in page['items'])
        self.assertEqual(len(ids), 2307)
        self.assertEqual(set(ids), set(range(1, 2308)))
        self.assertEqual(ids[:60], list(range(60, 0, -1)))

    def test_pending_filter_and_review_last_page(self):
        page = self.meme.get_meme_page(page=2)
        self.assertEqual((page['filtered_total'], page['pages'], len(page['items'])), (60, 2, 10))
        self.assertTrue(all(row['status'] == 'pending' for row in page['items']))
        for row in page['items']:
            self.meme.review_meme(row['id'], 'approved')
        page = self.meme.get_meme_page(page=2)
        self.assertEqual((page['page'], page['pages'], page['pending'], len(page['items'])), (1, 1, 50, 50))
        self.assertEqual(page['total'], 2307)

    def test_search_fields_literal_matching_and_late_records(self):
        with sqlite3.connect(self.db) as con:
            con.execute("INSERT INTO meme_alias(alias_key, display_name, meme_group_id) VALUES ('alias', '别名Alias', 1)")
            con.execute("UPDATE meme_entry SET content='Hello 100%_世界', adder_uid=987654 WHERE id=1")
            con.execute("UPDATE meme_entry SET content='private-path', entry_type='image' WHERE id=2")
        for query in ('测试', '别名alias'):
            result = self.meme.get_meme_page(query=query, status='all', page=41)
            self.assertEqual(result['filtered_total'], 2307)
            self.assertEqual(len(result['items']), 50)
        for query in ('hello', '100%_', '987654'):
            result = self.meme.get_meme_page(query=query, status='all')
            self.assertEqual([r['id'] for r in result['items']], [1])
        self.assertEqual(self.meme.get_meme_page(query='98765', status='all')['filtered_total'], 0)
        self.assertEqual(self.meme.get_meme_page(query='private-path', status='all')['filtered_total'], 0)
        result = self.meme.get_meme_page(query='内容2306', status='all')
        self.assertEqual([r['id'] for r in result['items']], [2307])
        self.assertEqual(self.meme.get_meme_page(query='内容2306')['filtered_total'], 0)
        self.assertEqual(self.meme.get_meme_page(query="' OR 1=1 --", status='all')['items'], [])
        self.assertEqual(self.meme.get_meme_page(query='  ', status='all')['filtered_total'], 2307)

    def test_deleted_last_page_clamps(self):
        last = self.meme.get_meme_page(page=47, status='all')
        self.assertEqual(len(last['items']), 7)
        for row in last['items']:
            self.meme.remove_meme(row['id'])
        result = self.meme.get_meme_page(page=47, status='all')
        self.assertEqual((result['page'], result['pages'], result['total']), (46, 46, 2300))

    def test_empty_and_invalid_input(self):
        self.assertEqual(self.meme.get_meme_page(page=-10)['page'], 1)
        self.assertEqual(self.meme.get_meme_page(page=10**100)['page'], 2)
        self.assertEqual(self.meme.get_meme_page(page_size=10**10)['page_size'], 200)
        with self.assertRaises(ValueError):
            self.meme.get_meme_page(status='invalid')
        with sqlite3.connect(self.db) as con:
            con.execute('DELETE FROM meme_entry')
        result = self.meme.get_meme_page(page=100, status='all')
        self.assertEqual((result['total'], result['pending'], result['page'], result['pages']), (0, 0, 1, 1))
        self.assertEqual(result['items'], [])

    def make_client(self):
        app = Flask(__name__, template_folder=str(ROOT / 'src/web/templates'))
        app.secret_key = 'test-only'
        app.add_url_rule('/', 'index', lambda: '')
        app.add_url_rule('/login', 'login_page', lambda: '')
        app.add_url_rule('/meme-images/<int:record_id>', 'meme_image', lambda record_id: '')
        # Exercise the real view and authentication functions without importing
        # app.py's production startup (gevent, state DB, scheduler, etc.).
        source = ast.parse((ROOT / 'src/web/app.py').read_text())
        nodes = [n for n in source.body if isinstance(n, ast.FunctionDef)
                 and n.name in {'login_required', '_get_csrf_token', 'meme_review_page'}]
        namespace = dict(app=app, secrets=secrets, wraps=wraps, session=session,
                         request=request, jsonify=jsonify, redirect=redirect, url_for=url_for,
                         render_template=render_template, get_meme_page=self.meme.get_meme_page)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'meme_review_view', 'exec'), namespace)
        return app.test_client()

    def test_view_real_totals_links_and_auth(self):
        client = self.make_client()
        self.assertEqual(client.get('/meme-review').status_code, 302)
        with client.session_transaction() as sess:
            sess['logged_in'] = True
        page = client.get('/meme-review?status=all&page=41').get_data(as_text=True)
        self.assertIn('共 2307 条语录', page)
        self.assertIn('第 41 / 47 页', page)
        self.assertEqual(page.count('<article class="card"'), 50)
        self.assertIn('status=all&amp;page=42', page)
        self.assertIn('window.location.reload()', page)
        page = client.get('/meme-review?status=all&page=999').get_data(as_text=True)
        self.assertIn('第 47 / 47 页', page)
        self.assertEqual(page.count('<article class="card"'), 7)
        page = client.get('/meme-review?status=bad&page=nope').get_data(as_text=True)
        self.assertIn('共 60 条待审核语录', page)
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE meme_entry SET content='<script>bad</script>' WHERE id=60")
        page = client.get('/meme-review').get_data(as_text=True)
        self.assertIn('&lt;script&gt;bad&lt;/script&gt;', page)
        self.assertNotIn('<script>bad</script>', page)

    def test_search_view_defaults_all_and_preserves_query(self):
        client = self.make_client()
        with client.session_transaction() as sess:
            sess['logged_in'] = True
        page = client.get('/meme-review?q=内容').get_data(as_text=True)
        self.assertIn('搜索“内容”匹配 2307 条', page)
        self.assertIn('第 1 / 47 页', page)
        self.assertIn('name="q" value="内容"', page)
        self.assertIn('page=2&amp;q=', page)
        page = client.get('/meme-review?q=内容&status=pending').get_data(as_text=True)
        self.assertIn('匹配 60 条', page)
        self.assertIn('第 1 / 2 页', page)
        page = client.get('/meme-review?q=不存在&page=100').get_data(as_text=True)
        self.assertIn('没有匹配的语录', page)
        self.assertIn('第 1 / 1 页', page)
        page = client.get('/meme-review', query_string={'q':'"><script>bad</script>'}).get_data(as_text=True)
        self.assertNotIn('<script>bad</script>', page)


if __name__ == '__main__':
    unittest.main()
