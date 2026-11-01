"""按网页订阅共享独立监听进程；失联页面自动释放连接。"""
import json
import subprocess
import sys
import time

from flask import request, render_template
from src.common.utils import ROOT
from src.db.subscription import list_subscribed_room_ids
from src.db.liver import get_name_by_roomid


def monitored_rooms():
    return [{'room_id': r, 'name': get_name_by_roomid(r) or str(r)} for r in list_subscribed_room_ids()]


class AlarmHub:
    def __init__(self, socketio):
        self.io = socketio
        self.clients = {}
        self.workers = {}
        self.sweeping = False

    def send(self, sid, room, data):
        self.io.emit('alarm_event', {'room_id': room, **data}, to=sid)

    def stop(self, sid):
        self.clients.pop(sid, None)
        for room, worker in list(self.workers.items()):
            if not any(c['room'] == room for c in self.clients.values()):
                self.workers.pop(room)
                if worker['process'].poll() is None:
                    worker['process'].terminate()

    def subscribe(self, sid, room):
        if room not in list_subscribed_room_ids():
            return {'ok': False, 'error': '请选择当前监控中的直播间'}
        if sid in self.clients and self.clients[sid]['room'] == room:
            self.clients[sid]['seen'] = time.monotonic()
            return {'ok': True}
        self.stop(sid)
        self.clients[sid] = {'room': room, 'seen': time.monotonic()}
        if room not in self.workers:
            process = subprocess.Popen([sys.executable, '-m', 'src.web.live_alarm_worker', str(room)],
                                       cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       text=True, start_new_session=True)
            worker = {'process': process, 'state': None}
            self.workers[room] = worker
            self.io.start_background_task(self.read, room, worker)
        elif self.workers[room]['state']:
            self.send(sid, room, self.workers[room]['state'])
        if not self.sweeping:
            self.sweeping = True
            self.io.start_background_task(self.sweep)
        return {'ok': True}

    def read(self, room, worker):
        process = worker['process']
        try:
            for line in process.stdout:
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                if self.workers.get(room) is not worker:
                    break
                if data.get('kind') == 'state':
                    worker['state'] = data
                for sid, entry in list(self.clients.items()):
                    if entry['room'] == room:
                        self.send(sid, room, data)
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            if self.workers.get(room) is worker:
                self.workers.pop(room)
                for sid, entry in list(self.clients.items()):
                    if entry['room'] == room:
                        self.send(sid, room, {'kind': 'reconnecting'})
                        self.clients.pop(sid)

    def sweep(self):
        try:
            while self.clients:
                self.io.sleep(15)
                allowed = set(list_subscribed_room_ids())
                for sid, entry in list(self.clients.items()):
                    if time.monotonic() - entry['seen'] > 60 or entry['room'] not in allowed:
                        self.stop(sid)
        finally:
            self.sweeping = False


def register_live_alarm(app, socketio, security, csrf_token):
    hub = AlarmHub(socketio)

    @app.get('/live-alarm')
    def live_alarm_page():
        return render_template('live_alarm.html', rooms=monitored_rooms(), csrf_token=csrf_token())

    @socketio.on('alarm_subscribe')
    def subscribe(data):
        if not security.authenticated() or request.sid not in security.sockets:
            return {'ok': False, 'error': '请重新登录'}
        try:
            room = int(data['room_id'])
        except (KeyError, TypeError, ValueError):
            return {'ok': False, 'error': '无效直播间'}
        return hub.subscribe(request.sid, room)

    @socketio.on('alarm_heartbeat')
    def heartbeat():
        if not security.authenticated():
            hub.stop(request.sid)
            return {'ok': False}
        entry = hub.clients.get(request.sid)
        if entry:
            entry['seen'] = time.monotonic()
        return {'ok': True, 'subscribed': entry is not None}

    @socketio.on('alarm_stop')
    def stop():
        hub.stop(request.sid)

    @socketio.on('alarm_test')
    def test():
        if security.authenticated() and request.sid in hub.clients:
            entry = hub.clients[request.sid]
            hub.send(request.sid, entry['room'], {'kind': 'test'})

    @socketio.on('disconnect')
    def disconnected(reason=None):
        hub.stop(request.sid)
        security.disconnected(reason)

    return hub
