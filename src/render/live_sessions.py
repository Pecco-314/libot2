import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from nonebot_plugin_imageutils import BuildImage
from src.render.emoji_text import Text2Image, prefetch_emoji_assets

from src.common.utils import ROOT, truncate_name

async def fetch_live_sessions_data(room_id: int, month: str) -> dict[str, Any]:
    url = f"https://dc.hihivr.top/gift/live_sessions?room_id={room_id}&union=VirtuaReal&month={month}"
    async with httpx.AsyncClient() as client:
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
        ("弹幕数", 100),
        ("礼物收入", 130),
        ("大航海", 130),
        ("SC收入", 130),
        ("总营收", 140)
    ]
    padding = 40
    width = sum(c[1] for c in cols) + padding * 2
    row_h = 50
    header_h = 60
    footer_h = 50
    
    # 计算总计数据
    total_duration = sum(item.get("duration_minutes", 0) for item in sessions)
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
        
        danmaku = str(item["danmaku_count"])
        Text2Image.from_text(danmaku, 24, fill=(80, 80, 80)).draw_on_image(canvas.image, (x_offset, y))
        x_offset += cols[3][1]
        
        gift = item["gift"]
        Text2Image.from_text(f"￥{gift:.2f}", 24, fill=(80, 80, 80)).draw_on_image(canvas.image, (x_offset, y))
        x_offset += cols[4][1]
        
        guard = item["guard"]
        Text2Image.from_text(f"￥{guard:.2f}", 24, fill=(80, 80, 80)).draw_on_image(canvas.image, (x_offset, y))
        x_offset += cols[5][1]
        
        sc = item["super_chat"]
        Text2Image.from_text(f"￥{sc:.2f}", 24, fill=(80, 80, 80)).draw_on_image(canvas.image, (x_offset, y))
        x_offset += cols[6][1]
        
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
    
    Text2Image.from_text(str(total_danmaku), 24, weight="bold", fill=(80, 80, 80)).draw_on_image(canvas.image, (x_offset, y))
    x_offset += cols[3][1]
    
    Text2Image.from_text(f"￥{total_gift:.2f}", 24, weight="bold", fill=(80, 80, 80)).draw_on_image(canvas.image, (x_offset, y))
    x_offset += cols[4][1]
    
    Text2Image.from_text(f"￥{total_guard:.2f}", 24, weight="bold", fill=(80, 80, 80)).draw_on_image(canvas.image, (x_offset, y))
    x_offset += cols[5][1]
    
    Text2Image.from_text(f"￥{total_sc:.2f}", 24, weight="bold", fill=(80, 80, 80)).draw_on_image(canvas.image, (x_offset, y))
    x_offset += cols[6][1]
    
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