import math
import re
from bisect import bisect_left, bisect_right
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from nonebot_plugin_imageutils import BuildImage
from src.render.emoji_text import Text2Image, prefetch_emoji_assets

from src.common.utils import ROOT, truncate_name
from src.db.event import get_live_end_near_start, list_online_counts


def _backfill_latest_duration(room_id: int, sessions: list[dict[str, Any]]) -> None:
    """Use the local PREPARING event when the upstream latest session is unfinished."""
    if not sessions:
        return

    def parse_start(item: dict[str, Any]) -> datetime:
        try:
            return datetime.strptime(str(item.get("start_time") or ""), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return datetime.min

    latest = max(sessions, key=parse_start)
    if latest.get("duration_minutes") or latest.get("end_time"):
        return

    upstream_start = parse_start(latest)
    if upstream_start == datetime.min:
        return

    local_start = int(upstream_start.timestamp())
    local_end = get_live_end_near_start(room_id, local_start)
    if local_end is None or local_end <= local_start:
        return

    latest["duration_minutes"] = max(1, (local_end - local_start) // 60)


def _parse_session_time(value: Any) -> int | None:
    try:
        return int(datetime.strptime(str(value or ""), "%Y-%m-%d %H:%M:%S").timestamp())
    except (TypeError, ValueError):
        return None


def _prefer_local_concurrency(room_id: int, sessions: list[dict[str, Any]]) -> None:
    """Replace upstream concurrency with our samples when a session has local data."""
    ranges: list[tuple[dict[str, Any], int, int]] = []
    now_ts = int(datetime.now().timestamp())
    for item in sessions:
        start_ts = _parse_session_time(item.get("start_time"))
        if start_ts is None:
            continue

        end_ts = _parse_session_time(item.get("end_time"))
        if end_ts is None:
            duration_minutes = int(item.get("duration_minutes") or 0)
            end_ts = start_ts + duration_minutes * 60 if duration_minutes else now_ts
        if end_ts < start_ts:
            continue
        ranges.append((item, start_ts, end_ts))

    if not ranges:
        return

    records = list_online_counts(
        room_id,
        min(start_ts for _, start_ts, _ in ranges),
        max(end_ts for _, _, end_ts in ranges),
    )
    timestamps = [int(record["timestamp"]) for record in records]
    values = [int(record["count"]) for record in records]

    for item, start_ts, end_ts in ranges:
        left = bisect_left(timestamps, start_ts)
        right = bisect_right(timestamps, end_ts)
        local_values = values[left:right]
        if local_values:
            item["avg_concurrency"] = sum(local_values) / len(local_values)
            item["max_concurrency"] = max(local_values)
            item["concurrency_source"] = "local"
        else:
            item["concurrency_source"] = "api"


async def fetch_live_sessions_data(room_id: int, month: str) -> dict[str, Any]:
    url = f"https://dc.hihivr.top/gift/live_sessions?room_id={room_id}&union=VirtuaReal&month={month}"
    async with httpx.AsyncClient(trust_env=False) as client:
        resp = await client.get(url, timeout=15)
        resp.raise_for_status()
        return resp.json()

def _format_duration(minutes: int) -> str:
    if not minutes:
        return "0小时0分"
    h = minutes // 60
    m = minutes % 60
    return f"{h}小时{m}分"

async def render_live_sessions_image(room_id: int, month: str) -> Path | None:
    data = await fetch_live_sessions_data(room_id, month)
    sessions = data.get("sessions", [])
    if not sessions:
        return None
    _backfill_latest_duration(room_id, sessions)
    _prefer_local_concurrency(room_id, sessions)

    await prefetch_emoji_assets([
        *(str(item.get("title") or "") for item in sessions),
    ])

    queried_user = data.get("queried_user", str(room_id))
    refresh_time = data.get("refresh_time", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    save_dir = ROOT / "data" / "images" / "live_sessions"
    save_dir.mkdir(parents=True, exist_ok=True)
    
    cols = [
        ("开播时间", 240),
        ("标题", 300),
        ("直播时长", 160),
        ("平均同接", 120),
        ("最高同接", 120),
        ("弹幕数", 100),
        ("礼物收入", 150),
        ("大航海", 160),
        ("SC收入", 150),
        ("总营收", 170),
    ]
    padding = 40
    width = sum(c[1] for c in cols) + padding * 2
    row_h = 50
    header_h = 60
    footer_h = 50
    
    # 计算总计数据
    total_duration = sum(item.get("duration_minutes", 0) for item in sessions)
    avg_concurrency_values = [
        float(value)
        for item in sessions
        if (value := item.get("avg_concurrency")) is not None
    ]
    max_concurrency_values = [
        int(value)
        for item in sessions
        if (value := item.get("max_concurrency")) is not None
    ]
    monthly_avg_concurrency = (
        round(sum(avg_concurrency_values) / len(avg_concurrency_values))
        if avg_concurrency_values
        else None
    )
    monthly_max_concurrency = max(max_concurrency_values, default=None)
    total_danmaku = sum(item.get("danmaku_count", 0) for item in sessions)
    total_gift = sum(item.get("gift", 0.0) for item in sessions)
    total_guard = sum(item.get("guard", 0.0) for item in sessions)
    total_sc = sum(item.get("super_chat", 0.0) for item in sessions)
    total_revenue = sum(item.get("total_revenue", 0.0) for item in sessions)

    # 画布高度增加一行总计行（len(sessions) + 1）
    height = header_h + (len(sessions) + 1) * row_h + footer_h + padding * 2
    canvas = BuildImage.new("RGBA", (width, height), (255, 255, 255, 255))
    
    y = padding
    x_offset = padding
    
    # 绘制表头
    for col_name, col_w in cols:
        t2i = Text2Image.from_text(col_name, 28, weight="bold", fill=(50, 50, 50))
        t2i.draw_on_image(canvas.image, (x_offset, y))
        x_offset += col_w
    
    y += header_h
    
    # 绘制每一行明细
    for item in sessions:
        x_offset = padding
        
        start_time = item["start_time"]
        Text2Image.from_text(start_time, 24, fill=(80, 80, 80)).draw_on_image(canvas.image, (x_offset, y))
        x_offset += cols[0][1]
        
        title = truncate_name(item["title"] or "无标题", max_len=20)
        Text2Image.from_text(title, 24, fill=(30, 30, 30)).draw_on_image(canvas.image, (x_offset, y))
        x_offset += cols[1][1]
        
        duration = _format_duration(item["duration_minutes"])
        Text2Image.from_text(duration, 24, fill=(80, 80, 80)).draw_on_image(canvas.image, (x_offset, y))
        x_offset += cols[2][1]

        avg_concurrency = item.get("avg_concurrency")
        avg_concurrency_text = "-" if avg_concurrency is None else str(round(float(avg_concurrency)))
        Text2Image.from_text(avg_concurrency_text, 24, fill=(80, 80, 80)).draw_on_image(canvas.image, (x_offset, y))
        x_offset += cols[3][1]

        max_concurrency = item.get("max_concurrency")
        max_concurrency_text = "-" if max_concurrency is None else str(int(max_concurrency))
        Text2Image.from_text(max_concurrency_text, 24, fill=(80, 80, 80)).draw_on_image(canvas.image, (x_offset, y))
        x_offset += cols[4][1]
        
        danmaku = str(item["danmaku_count"])
        Text2Image.from_text(danmaku, 24, fill=(80, 80, 80)).draw_on_image(canvas.image, (x_offset, y))
        x_offset += cols[5][1]
        
        gift = item["gift"]
        Text2Image.from_text(f"￥{gift:.2f}", 24, fill=(80, 80, 80)).draw_on_image(canvas.image, (x_offset, y))
        x_offset += cols[6][1]
        
        guard = item["guard"]
        Text2Image.from_text(f"￥{guard:.2f}", 24, fill=(80, 80, 80)).draw_on_image(canvas.image, (x_offset, y))
        x_offset += cols[7][1]
        
        sc = item["super_chat"]
        Text2Image.from_text(f"￥{sc:.2f}", 24, fill=(80, 80, 80)).draw_on_image(canvas.image, (x_offset, y))
        x_offset += cols[8][1]
        
        revenue = item["total_revenue"]
        Text2Image.from_text(f"￥{revenue:.2f}", 24, fill=(30, 30, 30)).draw_on_image(canvas.image, (x_offset, y))
        
        y += row_h

    # 绘制总计行
    x_offset = padding
    
    Text2Image.from_text("（总计）", 24, weight="bold", fill=(30, 30, 30)).draw_on_image(canvas.image, (x_offset, y))
    x_offset += cols[0][1]
    
    # 标题列在总计行留空
    x_offset += cols[1][1]
    
    Text2Image.from_text(_format_duration(total_duration), 24, weight="bold", fill=(80, 80, 80)).draw_on_image(canvas.image, (x_offset, y))
    x_offset += cols[2][1]

    monthly_avg_text = "-" if monthly_avg_concurrency is None else str(monthly_avg_concurrency)
    Text2Image.from_text(monthly_avg_text, 24, weight="bold", fill=(80, 80, 80)).draw_on_image(canvas.image, (x_offset, y))
    x_offset += cols[3][1]

    monthly_max_text = "-" if monthly_max_concurrency is None else str(monthly_max_concurrency)
    Text2Image.from_text(monthly_max_text, 24, weight="bold", fill=(80, 80, 80)).draw_on_image(canvas.image, (x_offset, y))
    x_offset += cols[4][1]
    
    Text2Image.from_text(str(total_danmaku), 24, weight="bold", fill=(80, 80, 80)).draw_on_image(canvas.image, (x_offset, y))
    x_offset += cols[5][1]
    
    Text2Image.from_text(f"￥{total_gift:.2f}", 24, weight="bold", fill=(80, 80, 80)).draw_on_image(canvas.image, (x_offset, y))
    x_offset += cols[6][1]
    
    Text2Image.from_text(f"￥{total_guard:.2f}", 24, weight="bold", fill=(80, 80, 80)).draw_on_image(canvas.image, (x_offset, y))
    x_offset += cols[7][1]
    
    Text2Image.from_text(f"￥{total_sc:.2f}", 24, weight="bold", fill=(80, 80, 80)).draw_on_image(canvas.image, (x_offset, y))
    x_offset += cols[8][1]
    
    Text2Image.from_text(f"￥{total_revenue:.2f}", 24, weight="bold", fill=(30, 30, 30)).draw_on_image(canvas.image, (x_offset, y))
    
    y += row_h

    # 底部说明小字
    y += 20
    footer_text = "数据来自dc.hihivr.top，感谢作者"
    footer_t2i = Text2Image.from_text(footer_text, 20, fill=(180, 180, 180))
    footer_x = width - padding - footer_t2i.width
    footer_t2i.draw_on_image(canvas.image, (footer_x, y))
    
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    filename = f"livesessions_{room_id}_{month}_{timestamp}.png"
    out_path = save_dir / filename
    canvas.image.save(out_path)
    
    return out_path
