"""SQLite 窗口缓存：每个已计算窗口独立提交，计算期间不持有写事务。"""
import hashlib
import json
import logging
import sqlite3
import time
from pathlib import Path

from src.capture.guesser import guess_song_timeline, refresh_now_playing_matcher
from src.db.sqlite import connect_sqlite, DEFAULT_DB_PATH
from src.db.transcript import list_transcripts_in_range

logger = logging.getLogger(__name__)
SCHEMA = 3


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class SQLiteWindowCache(dict):
    def __init__(self, conn, room_id, session_start, catalog):
        super().__init__()
        self.conn = conn
        self.scope = (room_id, session_start, SCHEMA, catalog)
        self.hits = self.writes = 0
        for start, end, signature, results in conn.execute(
            'SELECT window_start,window_end,signature,results FROM preview_window '
            'WHERE room_id=? AND session_start=? AND version=? AND catalog=?', self.scope
        ):
            try:
                decoded = json.loads(results)
                if isinstance(decoded, list):
                    super().__setitem__(f'{start}:{end}', {'signature': signature, 'results': decoded})
            except (ValueError, TypeError):
                pass

    def __setitem__(self, key, value):
        start, end = map(int, key.split(':'))
        with self.conn:
            self.conn.execute(
                'INSERT INTO preview_window(room_id,session_start,version,catalog,window_start,window_end,signature,results) '
                'VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(room_id,session_start,window_start,window_end) DO UPDATE SET '
                'version=excluded.version,catalog=excluded.catalog,signature=excluded.signature,results=excluded.results,updated_at=CURRENT_TIMESTAMP',
                (*self.scope, start, end, value['signature'], json.dumps(value['results'], ensure_ascii=False)),
            )
        super().__setitem__(key, value)
        self.writes += 1
        if self.writes % 10 == 0:
            logger.info('预览歌单缓存进度 room=%s session=%s 已提交窗口=%s', *self.scope[:2], self.writes)


class PersistentPreviewRecognizer:
    def __init__(self, directory=None):
        self.path = Path(directory or DEFAULT_DB_PATH.parent) / 'song_preview.sqlite3'
        self.catalog = None

    def revision(self, room_id, start_ts, end_ts):
        with connect_sqlite() as conn:
            catalog = conn.execute('SELECT id,title,title_trans,original_singer,lyrics_cleaned,lyrics FROM song_info ORDER BY id').fetchall()
        signature = digest(catalog)
        if signature != self.catalog:
            refresh_now_playing_matcher()
            self.catalog = signature
        rows = list_transcripts_in_range(room_id, start_ts, end_ts + 2)
        return digest([SCHEMA, signature, rows])

    def __call__(self, room_id, start_ts, end_ts):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5)
        tick = time.monotonic()
        windows = None
        try:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('''CREATE TABLE IF NOT EXISTS preview_window (
                room_id INTEGER NOT NULL, session_start INTEGER NOT NULL,
                version INTEGER NOT NULL, catalog TEXT NOT NULL,
                window_start INTEGER NOT NULL, window_end INTEGER NOT NULL,
                signature TEXT NOT NULL, results TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(room_id,session_start,window_start,window_end))''')
            windows = SQLiteWindowCache(conn, room_id, start_ts, self.catalog or '')
            logger.info('预览歌单窗口查询 room=%s session=%s end=%s 已存窗口=%s', room_id, start_ts, end_ts, len(windows))
            return guess_song_timeline(room_id, start_ts, end_ts, window_cache=windows)
        finally:
            conn.close()
            logger.info('预览歌单窗口结束 room=%s 命中=%s 新算=%s 耗时=%.2fs', room_id,
                        windows.hits if windows is not None else 0,
                        windows.writes if windows is not None else 0, time.monotonic() - tick)
