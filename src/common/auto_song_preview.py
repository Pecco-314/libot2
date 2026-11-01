"""下播后的预览歌单队列；独立缓存库保存生成结果与分段发送进度。"""
import asyncio
from contextlib import contextmanager
import logging
import os
from pathlib import Path
import sqlite3
import time

from src.common.utils import ROOT
from src.common.song_preview import preview_cache, format_preview, DEFAULT_THRESHOLD
from src.common.long_message import split_text
from src.db.event import list_live_sessions

logger = logging.getLogger('libot.auto_song_preview')
WAIT_SECONDS = 180
MAX_AGE = 86400


class PreviewQueue:
    def __init__(self, path=None):
        self.path = Path(path or ROOT / 'data' / 'auto_song_preview.sqlite3')

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=3)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript('''
            CREATE TABLE IF NOT EXISTS activation (
                room INTEGER NOT NULL, target INTEGER NOT NULL, since INTEGER NOT NULL,
                PRIMARY KEY(room,target));
            CREATE TABLE IF NOT EXISTS jobs (
                room INTEGER NOT NULL, target INTEGER NOT NULL, start INTEGER NOT NULL,
                end INTEGER NOT NULL, payload TEXT, sent_chunks INTEGER NOT NULL DEFAULT 0,
                done INTEGER NOT NULL DEFAULT 0, retry_at INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(room,target,start));
            ''')
            with conn:
                yield conn
        finally:
            conn.close()

    def discover(self, room, target, sessions, now):
        with self.connect() as conn:
            conn.execute('INSERT OR IGNORE INTO activation VALUES(?,?,?)', (room, target, now))
            since = conn.execute('SELECT since FROM activation WHERE room=? AND target=?', (room,target)).fetchone()[0]
            for session in sessions:
                end = session['end_ts']
                if end is not None and end >= max(since, now-MAX_AGE):
                    conn.execute('INSERT OR IGNORE INTO jobs(room,target,start,end) VALUES(?,?,?,?)',
                                 (room, target, session['start_ts'], end))
            rows = conn.execute('SELECT * FROM jobs WHERE room=? AND target=? AND done=0 '
                                'AND retry_at<=? AND end BETWEEN ? AND ? ORDER BY start',
                                (room, target, now, now-MAX_AGE, now-WAIT_SECONDS)).fetchall()
            return [dict(row) for row in rows]

    def update(self, job, **values):
        allowed = {'payload', 'sent_chunks', 'done', 'retry_at'}
        if not values or not set(values) <= allowed:
            raise ValueError('invalid queue update')
        with self.connect() as conn:
            conn.execute('UPDATE jobs SET '+','.join(f'{k}=?' for k in values)
                         +' WHERE room=? AND target=? AND start=?',
                         (*values.values(), job['room'], job['target'], job['start']))


class AutoSongPreview:
    def __init__(self, queue=None):
        self.queue = queue or PreviewQueue()

    async def run(self, bot, room, target, *, now=None):
        now = int(time.time()) if now is None else now
        sessions = await asyncio.to_thread(list_live_sessions, room)
        jobs = await asyncio.to_thread(self.queue.discover, room, target, sessions, now)
        if bot is None:
            return
        for job in jobs:
            try:
                await self.deliver(bot, job, now)
            except Exception:
                await asyncio.to_thread(self.queue.update, job, retry_at=int(time.time())+300)
                logger.exception('自动预览失败，五分钟后重试 room=%s start=%s target=%s', room, job['start'], target)

    async def deliver(self, bot, job, now):
        from nonebot.adapters.onebot.v11 import Message, MessageSegment

        payload = job['payload']
        if payload is None:
            result = await asyncio.to_thread(preview_cache.get, job['room'], job['start'],
                                             job['end'], DEFAULT_THRESHOLD, now=now)
            if not result['has_asr'] or result['pending']:
                if now-job['end'] < 1800:
                    raise RuntimeError('等待ASR落库')
                await asyncio.to_thread(self.queue.update, job, done=1)
                logger.warning('自动预览跳过：半小时内无ASR room=%s start=%s', job['room'], job['start'])
                return
            if not result['entries']:
                await asyncio.to_thread(self.queue.update, job, done=1)
                logger.info('自动预览无歌曲，跳过发送 room=%s start=%s', job['room'], job['start'])
                return
            payload = '\n'.join(format_preview(job['start'], result['entries']))
            await asyncio.to_thread(self.queue.update, job, payload=payload)
        chunks = split_text(payload)
        # 先发送歌单，再单独@全体，确保提醒位于合并转发之外。
        for offset in range(job['sent_chunks'], len(chunks), 50):
            batch = chunks[offset:offset+50]
            nodes = [{'type': 'node', 'data': {'name': 'Libot', 'uin': bot.self_id,
                      'content': [MessageSegment.text(text)]}} for text in batch]
            await bot.call_api('send_group_forward_msg', group_id=job['target'], messages=nodes)
            await asyncio.to_thread(self.queue.update, job, sent_chunks=offset+len(batch))
        # 提醒采用至多一次投递：QQ配额、发送失败或回执丢失均不应导致跨日补发。
        # 先持久化完成状态，进程中断时也不会重复提醒已经收到歌单的群。
        await asyncio.to_thread(self.queue.update, job, done=1)
        try:
            await bot.call_api('send_group_msg', group_id=job['target'], message=Message([
                MessageSegment.at('all'),
                MessageSegment.text(f" 本场预览歌单已生成（房间{job['room']}，匹配分≥{DEFAULT_THRESHOLD*100:g}%），请核对后生成。"),
            ]))
        except Exception:
            logger.exception('自动预览已发送，@全体失败（不重试） room=%s start=%s target=%s',
                             job['room'], job['start'], job['target'])
        logger.info('自动预览发送完成 room=%s start=%s target=%s', job['room'], job['start'], job['target'])


auto_song_preview = AutoSongPreview()


def configured_target():
    group = os.getenv('SONG_PREVIEW_TEST_GROUP', '').strip()
    room = os.getenv('SONG_PREVIEW_AUTO_ROOM', '1967216004').strip()
    if not group.isdigit() or int(group) <= 0 or not room.isdigit() or int(room) <= 0:
        return None
    return int(room), int(group)
