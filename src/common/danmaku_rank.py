"""弹幕榜的日期区间和互动计数；日期区间使用北京时间、右端不包含。"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo
from src.common.time_args import is_date_arg, calendar_span, beijing_now

TZ = ZoneInfo("Asia/Shanghai")
USAGE = "用法：弹幕榜 [日期] [场次]，或 弹幕榜 2026 / 2026-08 / 近1月 / 起始日期 结束日期"


def parse_rank_period(text: str, now: datetime | None = None) -> tuple[int, int, str] | None:
    """单个日期及日期加场次交给直播场次解析；近N月按N×30天（含今天）。"""
    parts = re.sub(r"[至~～]", " ", text).split()
    if not parts:
        return None
    if len(parts) > 2:
        raise ValueError(USAGE)
    if is_date_arg(parts[0]) and (
        len(parts) == 1 or re.fullmatch(r"\d{1,3}", parts[1])
    ):
        return None
    now = beijing_now(now)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def calendar_period(value: str) -> tuple[datetime, datetime]:
        return calendar_span(value, now=now)

    try:
        if len(parts) == 2:
            start, _ = calendar_period(parts[0])
            last_start, end = calendar_period(parts[1])
            if last_start < start:
                raise ValueError(USAGE)
        elif match := re.fullmatch(r"近?([1-9]\d{0,3})(天|月)", parts[0]):
            days = int(match[1]) * (30 if match[2] == "月" else 1)
            start, end = today - timedelta(days=days - 1), today + timedelta(days=1)
        else:
            start, end = calendar_period(parts[0])
    except (ValueError, OverflowError):
        raise ValueError(USAGE) from None
    start_ts, end_ts = int(start.timestamp()), min(int(end.timestamp()), int(now.timestamp()) + 1)
    if start_ts >= end_ts:
        raise ValueError("日期范围无效，开始日期不能晚于今天或结束日期")
    last = datetime.fromtimestamp(end_ts - 1, TZ)
    return start_ts, end_ts, f"{start:%Y-%m-%d}～{last:%Y-%m-%d}"


def _count_special_tags(text: str) -> tuple[int, int, int]:
    if not text:
        return 0, 0, 0
    dc = 1 if "打call" in text else 0
    dc += 1 if text.count("三理") >= 3 else 0
    dc += 1 if text.count("理理") >= 3 and "晚安" not in text else 0
    gn = 1 if "晚安" in text else 0
    ky = 1 if text in ["看你", "[UPOWER_2030198123_看你]"] else 0
    return dc, gn, ky


def build_danmaku_rank_items(rows: Iterable[dict[str, Any]], limit: int = 20) -> list[dict[str, Any]]:
    user_map: dict[int, dict[str, Any]] = {}
    for row in rows:
        uid = int(row.get("uid") or 0)
        if uid == 0:
            continue
        uname = str(row.get("uname") or "")
        content = row.get("content")
        entry = user_map.setdefault(uid, {"uid": uid, "uname": uname, "count": 0, "dc": 0, "gn": 0, "ky": 0})
        if uname:
            entry["uname"] = uname
        entry["count"] += 1
        if isinstance(content, str):
            dc, gn, ky = _count_special_tags(content)
            entry["dc"] += dc
            entry["gn"] += gn
            entry["ky"] += ky
    ranked = sorted(user_map.values(), key=lambda x: (-x["count"], x["uname"]))
    return [
        {"rank": rank, **{key: entry[key] for key in ("uname", "count", "dc", "gn", "ky")}}
        for rank, entry in enumerate(ranked[:limit], start=1)
    ]
