"""管理页面统一认证、登录限速和实时日志连接生命周期。"""
import hashlib
import hmac
import secrets
import time
from collections import deque
from datetime import timedelta
from urllib.parse import urlsplit

from flask import jsonify, redirect, request, session, url_for
from flask_socketio import join_room


class WebSecurity:
    LOG_ROOM = 'authenticated-logs'

    def __init__(self, app, socketio, password_hash):
        self.socketio = socketio
        self.password_hash = password_hash
        self.failures = {}
        self.sockets = {}
        app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict',
                          PERMANENT_SESSION_LIFETIME=timedelta(days=3650),
                          SESSION_REFRESH_EACH_REQUEST=True)
        app.before_request(self.protect)
        app.after_request(self.headers)
        socketio.on_event('connect', self.connect)
        socketio.on_event('disconnect', self.disconnected)

    def version(self):
        return hashlib.sha256(str(self.password_hash()).encode()).hexdigest()

    def authenticated(self):
        return bool(session.get('logged_in') and session.get('login_id')
                    and hmac.compare_digest(str(session.get('auth_version', '')), self.version()))

    def csrf_valid(self, supplied):
        expected = session.get('csrf_token')
        return bool(expected and isinstance(supplied, str) and hmac.compare_digest(expected, supplied))

    def protect(self):
        if request.endpoint in {'login_page', 'static'}:
            return None
        if not self.authenticated():
            if request.path.startswith('/api/'):
                return jsonify(error='unauthorized'), 401
            return redirect(url_for('login_page', next=request.full_path.rstrip('?')))
        # 将有效的旧会话转为长期 Cookie，保留登录身份与 CSRF 令牌。
        session.pop('auth_until', None)
        session.permanent = True
        if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'}:
            supplied = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token')
            if not self.csrf_valid(supplied):
                return jsonify(error='invalid_csrf'), 403

    def headers(self, response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        if request.endpoint != 'static':
            response.headers['Cache-Control'] = 'no-store'
        return response

    def client_ip(self):
        # nginx 覆盖 X-Real-IP；仅接受来自本机反向代理的该字段。
        if request.remote_addr in {'127.0.0.1', '::1'}:
            return request.headers.get('X-Real-IP') or request.remote_addr
        return request.remote_addr or 'unknown'

    def allow_attempt(self):
        now = time.monotonic()
        for key in list(self.failures):
            attempts = self.failures[key]
            while attempts and attempts[0] <= now - 600:
                attempts.popleft()
            if not attempts:
                del self.failures[key]
        key = self.client_ip()
        if key not in self.failures and len(self.failures) >= 4096:
            return False
        attempts = self.failures.setdefault(key, deque())
        if len(attempts) >= 10:
            return False
        attempts.append(now)
        return True

    def login(self):
        self.logout()
        self.failures.pop(self.client_ip(), None)
        session.update(logged_in=True, login_id=secrets.token_urlsafe(32),
                       auth_version=self.version(),
                       csrf_token=secrets.token_urlsafe(32))
        session.permanent = True

    def logout(self):
        ident = session.get('login_id')
        for sid, entry in list(self.sockets.items()):
            if ident and entry['login_id'] == ident:
                self.socketio.server.disconnect(sid, namespace='/')
                self.sockets.pop(sid, None)
        session.clear()

    def invalidate_sockets(self):
        for sid in list(self.sockets):
            self.socketio.server.disconnect(sid, namespace='/')
            self.sockets.pop(sid, None)

    def connect(self, auth=None):
        if not self.authenticated() or not isinstance(auth, dict) or not self.csrf_valid(auth.get('csrf_token')):
            return False
        self.sockets[request.sid] = {'login_id': session['login_id']}
        join_room(self.LOG_ROOM)

    def disconnected(self, reason=None):
        self.sockets.pop(request.sid, None)

    def emit_log(self, data):
        self.socketio.emit('log_event', data, to=self.LOG_ROOM)


def safe_next(value):
    """登录后只跳转站内相对路径。"""
    value = value or '/'
    parsed = urlsplit(value)
    if not value.startswith('/') or value.startswith('//') or parsed.netloc or parsed.scheme or '\\' in value:
        return '/'
    return value
