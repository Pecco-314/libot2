"""演唱识别查询的北京时间解析。"""
from datetime import datetime
from zoneinfo import ZoneInfo
from src.common.time_args import parse_datetime

BEIJING = ZoneInfo("Asia/Shanghai")


def parse_song_query_time(text: str, *, now: datetime | None = None) -> int:
    """省略日期时使用北京当天，省略秒时使用00；空值表示当前时刻。"""
    return int(parse_datetime(text, now=now).timestamp())
