"""网页专用B站连接；仅输出状态，不写业务数据或发送群通知。"""
import asyncio
import http.cookies
import json
import sys
import time

import aiohttp
import blivedm
from blivedm.handlers import HandlerInterface
from src.common.bilibili_auth import get_bilibili_auth
from src.common.utils import load_env_file


def output(kind, **data):
    print(json.dumps({'kind': kind, **data}, ensure_ascii=False), flush=True)


class SignalHandler(HandlerInterface):
    def __init__(self):
        self.last_heartbeat = 0
        self.needs_snapshot = True

    def handle(self, client, command):
        cmd = str(command.get('cmd', '')).split(':')[0]
        if cmd == '_HEARTBEAT':
            now = time.monotonic()
            if now - self.last_heartbeat > 45:
                self.needs_snapshot = True
            self.last_heartbeat = now
            output('heartbeat', at=time.time())
        elif cmd in {'LIVE', 'PREPARING'}:
            output('state', live=cmd == 'LIVE', source='websocket')


async def watch(room_id):
    load_env_file()
    while True:
        try:
            cookies = http.cookies.SimpleCookie()
            for name, value in get_bilibili_auth().cookies.items():
                cookies[name] = value
                cookies[name]['domain'] = '.bilibili.com'
                cookies[name]['path'] = '/'
            async with aiohttp.ClientSession(trust_env=False, headers={
                'User-Agent': 'Mozilla/5.0', 'Referer': 'https://live.bilibili.com/',
                'Origin': 'https://live.bilibili.com', 'Accept-Encoding': 'gzip, deflate',
            }) as session:
                session.cookie_jar.update_cookies(cookies)
                handler = SignalHandler()
                client = blivedm.BLiveClient(room_id, session=session, heartbeat_interval=30)
                client.set_handler(handler)
                client.start()
                started = time.monotonic()
                output('connecting')
                try:
                    while time.monotonic() - (handler.last_heartbeat or started) < 100:
                        # 首次连接及心跳中断后校准一次，补偿断线期间错过的开播信号。
                        if handler.needs_snapshot:
                            handler.needs_snapshot = False
                            try:
                                async with session.get('https://api.live.bilibili.com/room/v1/Room/get_info',
                                                       params={'room_id': room_id}, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                                    resp.raise_for_status()
                                    data = await resp.json()
                                if data.get('code') != 0:
                                    raise ValueError('room status unavailable')
                                output('state', live=data['data']['live_status'] == 1, source='snapshot')
                            except Exception:
                                output('warning', message='状态校准暂时失败，继续等待开播信号')
                        await asyncio.sleep(5)
                finally:
                    await client.stop_and_close()
        except Exception:
            output('warning', message='B站连接失败，正在重连')
        output('reconnecting')
        await asyncio.sleep(5)


if __name__ == '__main__':
    asyncio.run(watch(int(sys.argv[1])))
