import io
import tempfile
import unittest
from functools import wraps
from pathlib import Path

from flask import Flask, abort, session
from PIL import Image

from src.web.paste import create_paste_blueprint


class PasteTests(unittest.TestCase):
    def test_private_upload_download_delete(self):
        def auth(fn):
            @wraps(fn)
            def wrapped(*args, **kwargs):
                if not session.get('logged_in'):
                    abort(401)
                return fn(*args, **kwargs)
            return wrapped

        with tempfile.TemporaryDirectory() as folder:
            app = Flask(__name__, template_folder=str(Path(__file__).resolve().parents[1] / 'src/web/templates'))
            app.secret_key = 'test-only'
            app.register_blueprint(create_paste_blueprint(folder, auth))
            client = app.test_client()
            self.assertEqual(client.get('/paste/').status_code, 401)
            with client.session_transaction() as sess:
                sess['logged_in'] = True
                sess['csrf_token'] = 'test'
            self.assertEqual(client.post('/paste/', data={'text': 'bad'}).status_code, 403)
            image = io.BytesIO()
            Image.new('RGB', (8, 8), 'red').save(image, 'PNG')
            payload = image.getvalue()
            image.seek(0)
            response = client.post('/paste/', data={'csrf_token': 'test', 'text': '<script>alert(1)</script>', 'file': (image, '../../image.png')})
            self.assertEqual(response.status_code, 303)
            page = client.get('/paste/')
            self.assertIn(b'&lt;script&gt;', page.data)
            self.assertNotIn(b'<script>alert(1)', page.data)
            ident = next(p.name for p in Path(folder).iterdir() if len(p.name) == 32)
            with client.get(f'/paste/{ident}/download') as response:
                self.assertEqual(response.data, payload)
            with client.get(f'/paste/{ident}/preview') as response:
                self.assertEqual(response.mimetype, 'image/jpeg')
            with client.get(f'/paste/{ident}/image') as response:
                self.assertEqual(response.mimetype, 'image/png')
                self.assertEqual(response.data, payload)
                self.assertTrue(response.headers['Content-Disposition'].startswith('inline;'))
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertIn(f'/paste/{ident}/image'.encode(), page.data)
            with client.session_transaction() as sess:
                sess.clear()
            self.assertEqual(client.get(f'/paste/{ident}/download').status_code, 401)
            self.assertEqual(client.get(f'/paste/{ident}/preview').status_code, 401)
            self.assertEqual(client.get(f'/paste/{ident}/image').status_code, 401)
            with client.session_transaction() as sess:
                sess.update(logged_in=True, csrf_token='test')
            self.assertEqual(client.post(f'/paste/{ident}/delete', data={'csrf_token': 'test'}).status_code, 303)
            self.assertFalse((Path(folder) / ident).exists())
            self.assertEqual(client.get(f'/paste/{ident}/download').status_code, 404)
            response = client.post('/paste/', data={'csrf_token': 'test', 'text': 'pasted note'}, headers={'Accept': 'application/json'})
            self.assertEqual(response.status_code, 201)
            self.assertTrue(response.json['id'])
            response = client.post('/paste/', data={'csrf_token': 'test', 'file': (io.BytesIO(b'<svg onload="alert(1)"></svg>'), 'fake.png')}, headers={'Accept': 'application/json'})
            self.assertEqual(client.get(f"/paste/{response.json['id']}/image").status_code, 415)


if __name__ == '__main__':
    unittest.main()
