import time
import unittest
from flask import Flask, jsonify
from flask_socketio import SocketIO
from src.web.security import WebSecurity, safe_next


class WebSecurityTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = 'test-secret'
        self.app.testing = True
        self.socket = SocketIO(self.app, async_mode='threading')
        self.password = 'hash-v1'
        self.security = WebSecurity(self.app, self.socket, lambda: self.password)
        self.app.add_url_rule('/login', 'login_page', lambda: 'login')
        for path in ['/qrcode', '/get_qrcode_img', '/paste/', '/meme-review', '/api/logs/monitor.log']:
            self.app.add_url_rule(path, path, lambda: 'private')
        self.app.add_url_rule('/api/change_password', 'change', lambda: jsonify(ok=True), methods=['POST'])
        self.app.add_url_rule('/logout', 'logout', lambda: (self.security.logout() or 'bye'))
        self.client = self.app.test_client()

    def login(self):
        with self.client.session_transaction() as s:
            s.update(logged_in=True, login_id='login-one', auth_until=time.time()+3600,
                     auth_version=self.security.version(), csrf_token='csrf')

    def test_private_routes_and_old_sessions(self):
        for p in ['/qrcode', '/get_qrcode_img', '/paste/', '/meme-review']:
            self.assertEqual(self.client.get(p).status_code, 302)
        self.assertEqual(self.client.get('/api/logs/monitor.log').status_code, 401)
        with self.client.session_transaction() as s:
            s['logged_in'] = True
        self.assertEqual(self.client.get('/qrcode').status_code, 302)
        self.login()
        self.assertEqual(self.client.get('/qrcode').status_code, 200)
        self.assertEqual(self.client.get('/qrcode').headers['Cache-Control'], 'no-store')
        self.password = 'hash-v2'
        self.assertEqual(self.client.get('/qrcode').status_code, 302)

    def test_socket_auth_and_logout_disconnect(self):
        anonymous = self.socket.test_client(self.app)
        self.assertFalse(anonymous.is_connected())
        self.login()
        missing = self.socket.test_client(self.app, flask_test_client=self.client)
        self.assertFalse(missing.is_connected())
        logged = self.socket.test_client(self.app, flask_test_client=self.client, auth={'csrf_token':'csrf'})
        self.assertTrue(logged.is_connected())
        self.security.emit_log({'filename':'test', 'message':'private'})
        self.assertEqual(logged.get_received()[0]['name'], 'log_event')
        self.client.get('/logout')
        self.assertFalse(logged.is_connected())

    def test_long_lived_socket_and_password_rotation(self):
        self.login()
        with self.client.session_transaction() as s:
            s['auth_until'] = 1
        response = self.client.get('/qrcode')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Expires=', response.headers['Set-Cookie'])
        with self.client.session_transaction() as s:
            self.assertTrue(s.permanent)
            self.assertNotIn('auth_until', s)
        logged = self.socket.test_client(self.app, flask_test_client=self.client, auth={'csrf_token':'csrf'})
        self.security.emit_log({'message':'private'})
        self.assertTrue(logged.is_connected())
        self.security.invalidate_sockets()
        self.assertFalse(logged.is_connected())

    def test_csrf_and_rate_limit(self):
        self.login()
        self.assertEqual(self.client.post('/api/change_password', json={}).status_code, 403)
        self.assertEqual(self.client.post('/api/change_password', json={}, headers={'X-CSRF-Token':'csrf'}).status_code, 200)
        with self.app.test_request_context('/login', environ_base={'REMOTE_ADDR':'1.2.3.4'}):
            for _ in range(10): self.assertTrue(self.security.allow_attempt())
            self.assertFalse(self.security.allow_attempt())
        self.assertEqual(safe_next('https://evil.test'), '/')
        self.assertEqual(safe_next('//evil.test'), '/')
        self.assertEqual(safe_next('/qrcode'), '/qrcode')

    def test_cross_origin_handshake_rejected(self):
        response = self.client.get('/socket.io/?EIO=4&transport=polling', headers={'Origin':'https://evil.test'})
        self.assertEqual(response.status_code, 400)
