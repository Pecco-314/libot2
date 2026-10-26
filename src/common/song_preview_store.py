"""按场次持久化 ASR 识别窗口；输入及歌库内容变化时自动失效。"""
import hashlib
import json
import logging
import os
from pathlib import Path

from src.capture.guesser import guess_song_timeline, refresh_now_playing_matcher
from src.db.sqlite import connect_sqlite, DEFAULT_DB_PATH
from src.db.transcript import list_transcripts_in_range

logger = logging.getLogger(__name__)
SCHEMA = 1


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class PersistentPreviewRecognizer:
    def __init__(self, directory=None):
        self.directory = Path(directory or DEFAULT_DB_PATH.parent / 'song_preview')
        self.catalog = None

    def revision(self, room_id, start_ts, end_ts):
        with connect_sqlite() as conn:
            catalog = conn.execute('SELECT id,title,title_trans,original_singer,lyrics_cleaned FROM song_info ORDER BY id').fetchall()
        signature = digest(catalog)
        if signature != self.catalog:
            refresh_now_playing_matcher()
            self.catalog = signature
        rows = list_transcripts_in_range(room_id, start_ts, end_ts + 2)
        return digest([SCHEMA, signature, rows])

    def __call__(self, room_id, start_ts, end_ts):
        path = self.directory / f'{int(room_id)}_{int(start_ts)}.json'
        windows = {}
        try:
            saved = json.loads(path.read_text())
            if saved.get('schema') == SCHEMA and saved.get('catalog') == self.catalog:
                windows = saved['windows']
                if not isinstance(windows, dict):
                    windows = {}
        except (OSError, ValueError, KeyError, TypeError):
            pass
        result = guess_song_timeline(room_id, start_ts, end_ts, window_cache=windows)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(f'.{os.getpid()}.tmp')
            temporary.write_text(json.dumps({'schema': SCHEMA, 'catalog': self.catalog, 'windows': windows}, ensure_ascii=False))
            temporary.replace(path)
        except OSError:
            logger.exception('预览歌单缓存保存失败 path=%s', path)
        return result
