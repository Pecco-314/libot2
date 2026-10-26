"""群表情回应的日期参数与表情名称。"""
import json
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo
from src.common.time_args import is_date_arg
from src.common.danmaku_rank import parse_rank_period

TZ = ZoneInfo("Asia/Shanghai")
USAGE = "用法：贴表情榜 / 被贴表情榜 [今天/昨天/7天/近7天/本月/2026-09/2026-09-01] [结束日期]，默认本月"


def top_emojis(counts, limit=10):
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]


def parse_period(text, now=None):
    value = text.strip() or "本月"
    if is_date_arg(value):
        value = f"{value} {value}"
    result = parse_rank_period(value, now)
    if result is None:
        raise ValueError(USAGE)
    start, end, label = result
    return start, end, label.replace("～", " ～ ")


@lru_cache(maxsize=1)
def emoji_names():
    return json.loads(Path(__file__).with_name("qq_emoji_names.json").read_text())


def emoji_label(emoji_id):
    emoji_id = str(emoji_id)
    name = emoji_names().get(emoji_id)
    if name:
        return f"[{name}]"
    if emoji_id.isdigit() and 1000 < int(emoji_id) <= 0x10FFFF:
        char = chr(int(emoji_id))
        import emoji
        if emoji.is_emoji(char) or emoji.is_emoji(char + "\ufe0f"):
            return char + ("\ufe0f" if not emoji.is_emoji(char) else "")
    return f"[表情{emoji_id}]"
