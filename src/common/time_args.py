"""命令时间参数的公共语法；业务范围、默认值和区间边界由调用方决定。"""
from __future__ import annotations

import re
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")
RELATIVE_DAYS = {"今天": 0, "今日": 0, "昨天": -1, "昨日": -1, "前天": -2}
DATE_PATTERN = r"(?:(\d{4})[-/.])?(\d{1,2})[-/.](\d{1,2})"


def beijing_now(now: datetime | None = None) -> datetime:
    current = now or datetime.now(BEIJING)
    return current.replace(tzinfo=BEIJING) if current.tzinfo is None else current.astimezone(BEIJING)


def is_date_arg(value: str) -> bool:
    """识别日期语法，非法日历日期仍交给解析器报错，纯数字留给 UID/数量。"""
    return value in RELATIVE_DAYS or re.fullmatch(DATE_PATTERN, value) is not None


def parse_date(value: str, *, now: datetime | None = None) -> datetime:
    current = beijing_now(now)
    value = value.strip()
    if value in RELATIVE_DAYS:
        return (current + timedelta(days=RELATIVE_DAYS[value])).replace(hour=0, minute=0, second=0, microsecond=0)
    match = re.fullmatch(DATE_PATTERN, value)
    if not match:
        raise ValueError("日期格式错误，支持 YYYY-MM-DD、MM-DD、今天、昨天、前天")
    year, month, day = match.groups()
    month = int(month)
    resolved_year = int(year) if year else current.year - int(current.month == 1 and month == 12)
    try:
        return datetime(resolved_year, month, int(day), tzinfo=BEIJING)
    except ValueError:
        raise ValueError("日期不存在，请检查年月日") from None


def normalize_date(value: str, *, now: datetime | None = None) -> str:
    return parse_date(value, now=now).strftime("%Y-%m-%d")


def parse_date_index(text: str) -> tuple[str | None, int | None]:
    """解析日期及从 1 开始的场次/序号，保留日期原文供业务选择解析时机。"""
    parts = text.split()
    if not parts:
        return None, None
    if len(parts) > 2 or (len(parts) == 2 and (not parts[1].isdigit() or int(parts[1]) < 1)):
        raise ValueError("参数格式：日期 [场次或序号（从1开始）]")
    return parts[0], int(parts[1]) if len(parts) == 2 else None


def parse_datetime(value: str, *, now: datetime | None = None, past_clock: bool = False) -> datetime:
    """省略秒取 00；past_clock 用于将单独钟点定位到最近过去的一天。"""
    current = beijing_now(now)
    value = value.strip()
    if not value:
        return current
    parts = value.replace("T", " ").split()
    if len(parts) not in (1, 2):
        raise ValueError("时间格式：日期 HH:MM[:SS] 或 HH:MM[:SS]")
    clock = parts[-1]
    if not re.fullmatch(r"\d{1,2}:\d{1,2}(?::\d{1,2})?", clock):
        raise ValueError("时间格式：日期 HH:MM[:SS] 或 HH:MM[:SS]")
    day = parse_date(parts[0], now=current) if len(parts) == 2 else current.replace(hour=0, minute=0, second=0, microsecond=0)
    hours, minutes, *seconds = map(int, clock.split(":"))
    try:
        target = day.replace(hour=hours, minute=minutes, second=seconds[0] if seconds else 0)
    except ValueError:
        raise ValueError("时间不存在，请检查时、分、秒") from None
    if past_clock and len(parts) == 1 and target > current:
        target -= timedelta(days=1)
    return target


def parse_duration(value: str) -> float:
    """解析可组合的中文时长，返回秒；范围限制由命令决定。"""
    value = re.sub(r"\s+", "", value)
    units = {"秒": 1, "分钟": 60, "分": 60, "小时": 3600, "时": 3600, "天": 86400, "日": 86400, "周": 604800}
    pattern = r"(\d+(?:\.\d+)?)(分钟|小时|秒|分|时|天|日|周)"
    if not re.fullmatch(f"(?:{pattern})+", value):
        raise ValueError("时间长度格式：5秒、3分钟、24小时、1天或1小时30分钟")
    seconds = sum(float(number) * units[unit] for number, unit in re.findall(pattern, value))
    if not math.isfinite(seconds):
        raise ValueError("时间长度过大")
    return seconds


def parse_month(value: str, *, now: datetime | None = None, short: bool = False) -> tuple[int, int]:
    current = beijing_now(now)
    value = value.strip()
    if value in {"", "本月", "上月"}:
        start = current.replace(day=1)
        if value == "上月":
            start -= timedelta(days=1)
        return start.year, start.month
    match = re.fullmatch(r"(\d{4})[-/]?(\d{2})", value) or re.fullmatch(r"(\d{4})[-/](\d{1,2})", value)
    if match:
        year, month = map(int, match.groups())
    elif short and re.fullmatch(r"\d{1,2}", value):
        year, month = current.year, int(value)
    else:
        raise ValueError("月份格式：YYYY-MM、YYYYMM、本月、上月")
    try:
        datetime(year, month, 1)
    except ValueError:
        raise ValueError("月份不存在，请检查年份和月份") from None
    return year, month


def calendar_span(value: str, *, now: datetime | None = None) -> tuple[datetime, datetime]:
    """单个年、月、日的完整北京时间区间，右端不包含。"""
    current = beijing_now(now)
    if value == "今年" or re.fullmatch(r"\d{4}", value):
        year = current.year if value == "今年" else int(value)
        return datetime(year, 1, 1, tzinfo=BEIJING), datetime(year + 1, 1, 1, tzinfo=BEIJING)
    if is_date_arg(value) and not re.fullmatch(r"\d{4}[-/]\d{1,2}", value):
        start = parse_date(value, now=current)
        return start, start + timedelta(days=1)
    year, month = parse_month(value, now=current)
    start = datetime(year, month, 1, tzinfo=BEIJING)
    return start, (start + timedelta(days=32)).replace(day=1)
