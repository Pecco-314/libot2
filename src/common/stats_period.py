"""统计趋势的北京时间范围；日期区间包含末日，SQL 使用右开区间。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from src.common.danmaku_rank import TZ, parse_rank_period
from src.common.time_args import is_date_arg


@dataclass(frozen=True)
class StatsPeriod:
    start_ts: int
    end_ts: int
    label: str
    days: int | None = None

    @property
    def use_current(self) -> bool:
        return self.days is not None


def parse_stats_period(text: str, now: datetime | None = None) -> StatsPeriod:
    """整数天数查询滚动窗口；四位裸数字表示年份，带“天”可明确指定天数。"""
    now = now or datetime.now(TZ)
    now = now.replace(tzinfo=TZ) if now.tzinfo is None else now.astimezone(TZ)
    text = text.strip()
    usage = "时间格式：30 / 近30天 / 2025 / 2025-08 / 2025-08-01 2025-08-31"
    rolling = re.fullmatch(r"(?:近)?([1-9]\d{0,4})(?:天|日)?", text or "1")
    month = re.fullmatch(r"近([1-9]\d{0,3})月", text)
    if month or (rolling and not re.fullmatch(r"\d{4}", text)):
        days = int(month[1]) * 30 if month else int(rolling[1])
        try:
            start = now - timedelta(days=days)
        except (ValueError, OverflowError):
            raise ValueError("查询天数过大") from None
        return StatsPeriod(int(start.timestamp()), int(now.timestamp()) + 1, f"近{days}天", days)

    text = re.sub(r"[至~～]", " ", text)
    if is_date_arg(text):
        text = f"{text} {text}"
    try:
        period = parse_rank_period(text, now)
    except ValueError:
        raise ValueError(f"日期范围无效，结束日期须不早于开始日期，开始日期不能晚于今天。\n{usage}") from None
    if period is None:
        raise ValueError(usage)
    return StatsPeriod(*period)
