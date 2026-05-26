from __future__ import annotations

import math
import uuid
from datetime import datetime
from typing import Any

from nonebot_plugin_imageutils import BuildImage, Text2Image

from src.common.utils import ROOT, truncate_name


def _format_price(value: int | None, *, is_thousandth: bool) -> str:
    if value is None:
        return ""
    price = value / 1000 if is_thousandth else value
    if price == int(price):
        return f"￥{int(price)}"
    return f"￥{price:.2f}"


def _format_event_text(event: dict[str, Any]) -> tuple[str, tuple[int, int, int], str | None]:
    cmd = str(event.get("cmd") or "")
    content = event.get("content")
    gift_name = event.get("gift_name")
    gift_num = event.get("gift_num")
    total_coin = event.get("total_coin")
    title = event.get("title")
    default_color = (80, 80, 80)
    merge_count = int(event.get("merge_count") or 1)
    merge_amount = event.get("merge_amount")
    suffix = f"（{merge_count}）" if merge_count > 1 else None

    if cmd == "DANMU_MSG":
        return str(content or ""), default_color, suffix
    if cmd == "SEND_GIFT":
        name = str(gift_name or "礼物")
        num = int(gift_num or 1)
        price_value = merge_amount if merge_amount is not None else total_coin
        price = _format_price(price_value, is_thousandth=True)
        price_text = f"（{price}）" if price else ""
        return f"送出了{name}*{num}{price_text}", (199, 52, 122), suffix
    if cmd == "GUARD_BUY":
        name = str(gift_name or "大航海")
        return f"开通了{name}", (230, 126, 34), suffix
    if cmd == "SUPER_CHAT_MESSAGE":
        price = _format_price(total_coin, is_thousandth=False)
        text_suffix = f"（{price}）：{content}" if price else f"：{content}" if content else ""
        return f"醒目留言{text_suffix}", (26, 78, 140), suffix
    if cmd == "LIVE":
        return "开播", default_color, suffix
    if cmd == "PREPARING":
        return "下播", default_color, suffix
    if cmd == "ROOM_CHANGE":
        return (f"改标题：{title}" if title else "改标题"), default_color, suffix
    if cmd == "ONLINE_RANK_COUNT":
        return (f"同接 {content}" if content is not None else "同接"), default_color, suffix

    return str(content or cmd), default_color, suffix


def _merge_events(events: list[dict[str, Any]], merge_window: int = 30) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    last_seen: dict[tuple[str, str], tuple[int, int]] = {}

    for event in events:
        cmd = str(event.get("cmd") or "")
        timestamp = int(event.get("timestamp") or 0)
        if cmd == "DANMU_MSG":
            key = (cmd, str(event.get("content") or ""))
        elif cmd == "SEND_GIFT":
            key = (cmd, str(event.get("gift_name") or ""))
        else:
            merged.append({**event, "merge_count": 1})
            continue

        if key in last_seen:
            index, last_ts = last_seen[key]
            if timestamp - last_ts <= merge_window:
                target = merged[index]
                target["merge_count"] = int(target.get("merge_count") or 1) + 1
                if cmd == "SEND_GIFT":
                    target["gift_num"] = int(target.get("gift_num") or 0) + int(event.get("gift_num") or 0)
                    last_amount = int(target.get("merge_amount") or target.get("total_coin") or 0)
                    target["merge_amount"] = last_amount + int(event.get("total_coin") or 0)
                last_seen[key] = (index, timestamp)
                continue

        merged.append({**event, "merge_count": 1})
        last_seen[key] = (len(merged) - 1, timestamp)

    return merged


def render_event_pages(title: str, events: list[dict[str, Any]], page_size: int = 100) -> list[str]:
    if not events:
        return []

    events = _merge_events(events)

    font_size = 14
    line_height = int(font_size * 1.6)
    width = 980
    padding = 30
    gutter = 20
    column_width = (width - padding * 2 - gutter) // 2
    pages: list[str] = []
    total_pages = math.ceil(len(events) / page_size)

    for page_idx in range(total_pages):
        header_text = f"{title}（{page_idx + 1}/{total_pages}页）"
        header_t2i = Text2Image.from_text(header_text, 24, weight="bold", fill=(34, 34, 34))
        chunk = events[page_idx * page_size : (page_idx + 1) * page_size]
        left = chunk[: page_size // 2]
        right = chunk[page_size // 2 :]

        lines_count = max(len(left), len(right))
        content_h = padding + header_t2i.height + 16 + lines_count * line_height + padding

        canvas = BuildImage.new("RGBA", (width, int(content_h)), (255, 255, 255, 255))
        header_t2i.draw_on_image(canvas.image, (padding, padding))

        y = padding + header_t2i.height + 16
        for i in range(lines_count):
            if i < len(left):
                event = left[i]
                time_str = datetime.fromtimestamp(event["timestamp"]).strftime("%H:%M:%S") if event.get("timestamp") else "--:--:--"
                text, color, suffix = _format_event_text(event)
                text = truncate_name(text, max_len=48)
                line = f"{time_str}  {text}"
                base_img = Text2Image.from_text(line, font_size, fill=color)
                base_img.draw_on_image(canvas.image, (padding, y))
                if suffix:
                    Text2Image.from_text(suffix, font_size, fill=(160, 160, 160)).draw_on_image(
                        canvas.image, (padding + base_img.width + 4, y)
                    )
            if i < len(right):
                event = right[i]
                time_str = datetime.fromtimestamp(event["timestamp"]).strftime("%H:%M:%S") if event.get("timestamp") else "--:--:--"
                text, color, suffix = _format_event_text(event)
                text = truncate_name(text, max_len=48)
                line = f"{time_str}  {text}"
                base_img = Text2Image.from_text(line, font_size, fill=color)
                base_img.draw_on_image(canvas.image, (padding + column_width + gutter, y))
                if suffix:
                    Text2Image.from_text(suffix, font_size, fill=(160, 160, 160)).draw_on_image(
                        canvas.image, (padding + column_width + gutter + base_img.width + 4, y)
                    )
            y += line_height

        save_dir = ROOT / "data" / "images" / "events"
        save_dir.mkdir(parents=True, exist_ok=True)
        filename = f"events_{uuid.uuid4().hex[:8]}_{page_idx + 1}.png"
        save_path = save_dir / filename
        canvas.image.save(save_path)
        pages.append(str(save_path))

    return pages
