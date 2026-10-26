"""预览歌单参数及短期识别缓存，预留 ASR 落库时间。"""
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime
import math
import re
import threading
import time
from zoneinfo import ZoneInfo

from src.capture.guesser import collapse_song_timeline, guess_song_timeline
from src.common.time_args import parse_date_index

DEFAULT_THRESHOLD = .95
ASR_DELAY = 120
CACHE_TTL = 300
USAGE = '用法：预览歌单 [日期] [场次] [门槛]，例如：预览歌单 98% 或 预览歌单 2026-10-24 2 0.98'


def parse_preview_args(text):
    parts = text.split()
    threshold = DEFAULT_THRESHOLD
    if parts and ('%' in parts[-1] or re.fullmatch(r'(?:0?\.\d+|1\.0+)', parts[-1]) or parts[-1].startswith('门槛=') or len(parts) == 3):
        value = parts.pop().removeprefix('门槛=')
        percent = value.endswith('%')
        try:
            threshold = float(value.removesuffix('%'))
            if percent or threshold > 1:
                threshold /= 100
            if not math.isfinite(threshold) or not 0 <= threshold < 1:
                raise ValueError
        except ValueError:
            raise ValueError('门槛应为 0% 至小于100%，或 0 至小于1的小数') from None
    date, index = parse_date_index(" ".join(parts))
    return date, index, threshold


class SongPreviewCache:
    def __init__(self, recognizer=guess_song_timeline, *, revision=None):
        self.recognizer = recognizer
        self.revision = revision
        self.raw = OrderedDict()
        self.filtered = OrderedDict()
        self.lock = threading.Lock()

    def get(self, room_id, start_ts, end_ts, threshold, *, now=None):
        now = time.time() if now is None else now
        stable = int(now) - ASR_DELAY
        # 对仍在落库的尾部按开播起点对齐整分钟，避免每秒产生一个缓存键。
        if end_ts is None or end_ts > stable:
            end = start_ts + ((stable - start_ts) // 60) * 60
        else:
            end = int(end_ts)
        if end <= start_ts:
            return {'pending': True, 'entries': [], 'end_ts': end, 'has_asr': False}
        with self.lock:
            revision = self.revision(room_id, start_ts, end) if self.revision else None
            key = (room_id, start_ts, end, revision)
            tick = time.monotonic()
            for old in list(self.raw):
                if tick - self.raw[old][0] >= CACHE_TTL:
                    del self.raw[old]
            for old in list(self.filtered):
                if old[:4] not in self.raw:
                    del self.filtered[old]
            if key not in self.raw:
                timeline = self.recognizer(room_id, start_ts, end)
                self.raw[key] = (time.monotonic(), timeline)
            self.raw.move_to_end(key)
            timeline = self.raw[key][1]
            filtered_key = (*key, threshold)
            if filtered_key not in self.filtered:
                self.filtered[filtered_key] = collapse_song_timeline(
                    timeline, min_score=threshold, inclusive=True, max_gap_seconds=120,
                )
            self.filtered.move_to_end(filtered_key)
            result = deepcopy(self.filtered[filtered_key])
            while len(self.raw) > 16:
                self.raw.popitem(last=False)
            while len(self.filtered) > 64:
                self.filtered.popitem(last=False)
        return {'pending': False, 'entries': result, 'end_ts': end, 'has_asr': bool(timeline)}


from src.common.song_preview_store import PersistentPreviewRecognizer

_persistent_recognizer = PersistentPreviewRecognizer()
preview_cache = SongPreviewCache(_persistent_recognizer, revision=_persistent_recognizer.revision)


def format_preview(start_ts, entries):
    date = datetime.fromtimestamp(start_ts, ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d')
    # 歌手限定符兼容生成歌单的输入，避免同名歌曲选错。
    lines = [f'生成歌单 {date}']
    for item in entries:
        detected = datetime.fromtimestamp(item['first_ts'], ZoneInfo('Asia/Shanghai'))
        lines.append(f"{item['title']} | {item['singer']} #{detected:%H:%M:%S}")
    return lines
