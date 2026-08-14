import asyncio
import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from nonebot_plugin_imageutils import BuildImage
from src.render.emoji_text import Text2Image, prefetch_emoji_assets

from src.common.utils import ROOT, truncate_name


DC_BASE_URLS = {
    "vr": ("https://vr.qianqiuzy.cn",),
    "psp": ("https://psp.qianqiuzy.cn",),
    "all": ("https://vr.qianqiuzy.cn", "https://psp.qianqiuzy.cn"),
}


async def fetch_dc_data(filter_type: str, month: str) -> list[dict[str, Any]]:
    base_urls = DC_BASE_URLS.get(filter_type)
    if base_urls is None:
        raise ValueError(f"不支持的斗虫社团筛选：{filter_type}")

    async with httpx.AsyncClient(trust_env=False) as client:
        responses = await asyncio.gather(
            *(
                client.get(
                    f"{base_url}/gift/by_month",
                    params={"month": month},
                    timeout=30,
                )
                for base_url in base_urls
            )
        )

    anchors_by_room: dict[int, dict[str, Any]] = {}
    for response in responses:
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("斗虫接口响应不是数组")
        for raw_anchor in payload:
            if not isinstance(raw_anchor, dict):
                raise ValueError("斗虫接口主播数据格式错误")
            if raw_anchor.get("month") != month:
                raise ValueError(
                    f"斗虫接口返回月份 {raw_anchor.get('month')}，请求月份为 {month}"
                )

            anchor = dict(raw_anchor)
            anchor["total_revenue"] = sum(
                float(anchor.get(field) or 0)
                for field in ("gift", "guard", "super_chat")
            )
            room_id = int(anchor.get("room_id") or 0)
            if not room_id:
                continue
            existing = anchors_by_room.get(room_id)
            if (
                existing is None
                or anchor["total_revenue"] > existing["total_revenue"]
            ):
                anchors_by_room[room_id] = anchor

    return list(anchors_by_room.values())


def _parse_duration(duration_str: str) -> int:
    if not duration_str:
        return 0
    parts = duration_str.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    return 0

def _format_duration(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

async def get_dc_data(filter_type: str, time_str: str) -> list[dict[str, Any]]:
    months_to_fetch = []
    if len(time_str) == 4:
        now = datetime.now()
        year = int(time_str)
        end_month = 12 if year < now.year else now.month
        for m in range(1, end_month + 1):
            months_to_fetch.append(f"{year}{m:02d}")
    else:
        months_to_fetch.append(time_str.replace("-", ""))

    semaphore = asyncio.Semaphore(3)

    async def fetch_month(month: str) -> tuple[str, list[dict[str, Any]]]:
        async with semaphore:
            data = await fetch_dc_data(filter_type, month)
        if not data:
            raise RuntimeError(f"{month} 没有斗虫数据")
        return month, data

    month_results = await asyncio.gather(
        *(fetch_month(month) for month in months_to_fetch)
    )

    aggregated: dict[int, dict[str, Any]] = {}
    for month, data in month_results:
        logging.info("获取 %s 斗虫数据成功，共 %d 位主播", month, len(data))
        for anchor in data:
            room_id = anchor.get("room_id")
            if not room_id:
                continue
            if room_id not in aggregated:
                aggregated[room_id] = {
                    "anchor_name": anchor.get("anchor_name", "未知"),
                    "total_revenue": 0.0,
                    "live_duration_sec": 0,
                    "effective_days": 0,
                    "gift": 0.0,
                    "guard": 0.0,
                    "super_chat": 0.0,
                }
            aggregated[room_id]["anchor_name"] = anchor.get(
                "anchor_name", aggregated[room_id]["anchor_name"]
            )
            aggregated[room_id]["total_revenue"] += float(
                anchor.get("total_revenue") or 0
            )
            aggregated[room_id]["gift"] += float(anchor.get("gift") or 0)
            aggregated[room_id]["guard"] += float(anchor.get("guard") or 0)
            aggregated[room_id]["super_chat"] += float(
                anchor.get("super_chat") or 0
            )
            aggregated[room_id]["live_duration_sec"] += _parse_duration(
                anchor.get("live_duration", "00:00:00")
            )
            aggregated[room_id]["effective_days"] += int(
                anchor.get("effective_days") or 0
            )

    result = list(aggregated.values())
    result.sort(key=lambda x: x["total_revenue"], reverse=True)
    
    for i, item in enumerate(result):
        item["rank"] = i + 1
        item["live_duration"] = _format_duration(item["live_duration_sec"])
        
    return result

async def render_dc_images(filter_type: str, time_str: str, chunk_size: int = 25) -> list[Path]:
    data_list = await get_dc_data(filter_type, time_str)
    if not data_list:
        return []

    await prefetch_emoji_assets([
        *(str(item.get("anchor_name") or "") for item in data_list),
    ])

    save_dir = ROOT / "data" / "images" / "dc"
    save_dir.mkdir(parents=True, exist_ok=True)
    
    cols = [
        ("排名", 80),
        ("名称", 250),
        ("总营收", 190),
        ("直播时长", 140),
        ("有效天", 120),
        ("礼物", 190),
        ("大航海", 190),
        ("SC", 190),
    ]
    padding = 40
    width = sum(c[1] for c in cols) + padding * 2
    row_h = 50
    header_h = 60
    footer_h = 50
    
    total_chunks = max(1, math.ceil(len(data_list) / chunk_size))
    generated_paths = []
    
    for i in range(total_chunks):
        chunk_data = data_list[i * chunk_size : (i + 1) * chunk_size]
        height = header_h + len(chunk_data) * row_h + footer_h + padding * 2
        canvas = BuildImage.new("RGBA", (width, height), (255, 255, 255, 255))
        
        y = padding
        x_offset = padding
        
        # 绘制表头
        for col_name, col_w in cols:
            t2i = Text2Image.from_text(col_name, 28, weight="bold", fill=(50, 50, 50))
            t2i.draw_on_image(canvas.image, (x_offset, y))
            x_offset += col_w
        
        y += header_h
        
        # 绘制该页的行数据
        for item in chunk_data:
            x_offset = padding
            
            Text2Image.from_text(str(item["rank"]), 26, fill=(80, 80, 80)).draw_on_image(canvas.image, (x_offset, y))
            x_offset += cols[0][1]
            
            name = truncate_name(item["anchor_name"], max_len=24)
            Text2Image.from_text(name, 26, fill=(30, 30, 30)).draw_on_image(canvas.image, (x_offset, y))
            x_offset += cols[1][1]
            
            Text2Image.from_text(f"￥{item['total_revenue']:.2f}", 26, fill=(30, 30, 30)).draw_on_image(canvas.image, (x_offset, y))
            x_offset += cols[2][1]
            
            Text2Image.from_text(item["live_duration"], 26, fill=(80, 80, 80)).draw_on_image(canvas.image, (x_offset, y))
            x_offset += cols[3][1]
            
            Text2Image.from_text(str(item["effective_days"]), 26, fill=(80, 80, 80)).draw_on_image(canvas.image, (x_offset, y))
            x_offset += cols[4][1]
            
            Text2Image.from_text(f"￥{item['gift']:.2f}", 26, fill=(80, 80, 80)).draw_on_image(canvas.image, (x_offset, y))
            x_offset += cols[5][1]
            
            Text2Image.from_text(f"￥{item['guard']:.2f}", 26, fill=(80, 80, 80)).draw_on_image(canvas.image, (x_offset, y))
            x_offset += cols[6][1]
            
            Text2Image.from_text(f"￥{item['super_chat']:.2f}", 26, fill=(80, 80, 80)).draw_on_image(canvas.image, (x_offset, y))
            
            y += row_h

        # 绘制底部灰色小字
        y += 20
        footer_text = (
            f"数据来源千秋紫莹，感谢作者"
            f" | 第 {i+1}/{total_chunks} 页"
        )
        footer_t2i = Text2Image.from_text(footer_text, 20, fill=(180, 180, 180))
        footer_x = width - padding - footer_t2i.width
        footer_t2i.draw_on_image(canvas.image, (footer_x, y))
        
        # 保存图片
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        filename = f"dc_{filter_type}_{time_str}_{timestamp}_part{i+1}.png"
        out_path = save_dir / filename
        canvas.image.save(out_path)
        generated_paths.append(out_path)
        
    return generated_paths
