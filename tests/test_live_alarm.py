import unittest
from unittest.mock import Mock, patch

from flask import Flask
from flask_socketio import SocketIO
from src.web.security import WebSecurity
from src.web.live_alarm import register_live_alarm
from src.web.live_alarm_worker import SignalHandler
from src.common.utils import ROOT


class LiveAlarmTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder=str(ROOT / 'src/web/templates'))
        self.app.secret_key = 'test'
        self.socket = SocketIO(self.app, async_mode='threading')
        self.security = WebSecurity(self.app, self.socket, lambda: 'password-hash')
        self.app.add_url_rule('/login', 'login_page', lambda: 'login')
        self.app.add_url_rule('/logout', 'logout', lambda: (self.security.logout() or 'bye'))
        self.hub = register_live_alarm(self.app, self.socket, self.security, lambda: 'csrf')
        self.http = self.app.test_client()
        with self.http.session_transaction() as session:
            session.update(logged_in=True, login_id='one', auth_version=self.security.version(), csrf_token='csrf')
        self.rooms = patch('src.web.live_alarm.list_subscribed_room_ids', return_value=[123])
        self.spawn = patch('src.web.live_alarm.subprocess.Popen')
        self.tasks = patch.object(self.socket, 'start_background_task')
        self.rooms.start()
        self.process = self.spawn.start().return_value
        self.process.poll.return_value = None
        self.tasks.start()
        self.addCleanup(patch.stopall)

    def client(self):
        return self.socket.test_client(self.app, flask_test_client=self.http, auth={'csrf_token': 'csrf'})

    def test_requires_login_and_valid_room(self):
        self.assertFalse(self.socket.test_client(self.app).is_connected())
        self.assertEqual(self.app.test_client().get('/live-alarm').status_code, 302)
        client = self.client()
        self.assertFalse(client.emit('alarm_subscribe', {'room_id': 999}, callback=True)['ok'])
        self.assertFalse(client.emit('alarm_subscribe', {}, callback=True)['ok'])
        self.assertEqual(self.hub.workers, {})

    def test_page_shows_room_selector_and_isolated_test(self):
        with patch('src.web.live_alarm.monitored_rooms', return_value=[{'room_id': 123, 'name': '测试主播'}]):
            page = self.http.get('/live-alarm')
        self.assertEqual(page.status_code, 200)
        self.assertIn('测试主播', page.text)
        self.assertIn("transports:['websocket']", page.text)
        self.assertIn("socket.emit('alarm_test')", page.text)

    def test_shared_worker_and_cleanup(self):
        a, b = self.client(), self.client()
        self.assertTrue(a.emit('alarm_subscribe', {'room_id': 123}, callback=True)['ok'])
        b.emit('alarm_subscribe', {'room_id': 123})
        self.assertEqual(len(self.hub.workers), 1)
        self.assertEqual(len(self.hub.clients), 2)
        a.disconnect()
        self.process.terminate.assert_not_called()
        b.disconnect()
        self.process.terminate.assert_called_once()
        self.assertEqual(self.hub.workers, {})

    def test_simulation_is_private_and_not_real_state(self):
        a, b = self.client(), self.client()
        a.emit('alarm_subscribe', {'room_id': 123})
        b.emit('alarm_subscribe', {'room_id': 123})
        a.emit('alarm_test')
        event = a.get_received()[0]
        self.assertEqual(event['args'][0]['kind'], 'test')
        self.assertEqual(b.get_received(), [])
        self.assertIsNone(self.hub.workers[123]['state'])

    def test_heartbeat_and_logout(self):
        client = self.client()
        client.emit('alarm_subscribe', {'room_id': 123})
        self.assertTrue(client.emit('alarm_heartbeat', callback=True)['subscribed'])
        self.http.get('/logout')
        self.assertFalse(client.is_connected())
        self.assertEqual(self.hub.clients, {})

    def test_expired_browser_releases_worker(self):
        client = self.client()
        client.emit('alarm_subscribe', {'room_id': 123})
        for entry in self.hub.clients.values():
            entry['seen'] = 0
        with patch.object(self.socket, 'sleep'):
            self.hub.sweep()
        self.assertEqual(self.hub.workers, {})
        self.assertFalse(client.emit('alarm_heartbeat', callback=True)['subscribed'])

    def test_worker_only_forwards_live_signals_and_heartbeat(self):
        handler = SignalHandler()
        with patch('src.web.live_alarm_worker.output') as emit:
            handler.handle(None, {'cmd': 'DANMU_MSG'})
            emit.assert_not_called()
            handler.handle(None, {'cmd': 'LIVE'})
            emit.assert_called_with('state', live=True, source='websocket')
            handler.handle(None, {'cmd': 'PREPARING'})
            emit.assert_called_with('state', live=False, source='websocket')
            handler.handle(None, {'cmd': '_HEARTBEAT'})
            self.assertGreater(handler.last_heartbeat, 0)
            self.assertEqual(emit.call_args.args[0], 'heartbeat')


if __name__ == '__main__':
    unittest.main()
