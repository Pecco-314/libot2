"""群表情回应的日期参数与表情名称。"""
import json
import re
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
USAGE = "用法：贴表情榜 / 被贴表情榜 [今天/昨天/7天/近7天/本月/2026-09/2026-09-01] [结束日期]，默认本月"


def top_emojis(counts, limit=10):
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]


def parse_period(text, now=None):
    now = now or datetime.now(TZ)
    now = now.astimezone(TZ)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    parts = text.split() or ["本月"]
    if len(parts) > 2:
        raise ValueError(USAGE)

    def day(value):
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=TZ)

    try:
        value = parts[0]
        if len(parts) == 2:
            start, end = day(value), day(parts[1]) + timedelta(days=1)
        elif value in {"今天", "昨天"}:
            start = today - timedelta(days=value == "昨天")
            end = start + timedelta(days=1)
        elif value == "本月":
            start = today.replace(day=1)
            end = (start + timedelta(days=32)).replace(day=1)
        elif match := re.fullmatch(r"近?([1-9]\d{0,3})天", value):
            start, end = today - timedelta(days=int(match[1])-1), today + timedelta(days=1)
        elif re.fullmatch(r"\d{4}-\d{2}", value):
            start = day(value + "-01")
            end = (start + timedelta(days=32)).replace(day=1)
        else:
            start = day(value)
            end = start + timedelta(days=1)
    except (ValueError, OverflowError):
        raise ValueError(USAGE) from None
    if end <= start or start > now:
        raise ValueError("日期范围无效，开始日期不能晚于今天或结束日期")
    label = f"{start:%Y-%m-%d} ～ {min(end-timedelta(seconds=1), now):%Y-%m-%d}"
    return int(start.timestamp()), min(int(end.timestamp()), int(now.timestamp())+1), label


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
