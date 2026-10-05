from __future__ import annotations

import math
import uuid
import re
from datetime import datetime
from typing import Any

from nonebot_plugin_imageutils import BuildImage
from src.common.event_text import format_event_text as _format_event_text
from src.common.event_text import format_price as _format_price
from src.common.text import split_text_units
from src.render.emoji_text import Text2Image, prefetch_emoji_assets

from src.common.utils import ROOT


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


async def render_event_pages(
    title: str,
    events: list[dict[str, Any]],
    page_size: int = 100,
    show_date: bool = False,
    show_uname: bool = False,
    merge_events: bool = True,
) -> list[str]:
    if not events:
        return []

    await prefetch_emoji_assets([
        title,
        *(str(event.get("uname") or "") for event in events),
        *(str(event.get("content") or "") for event in events),
        *(str(event.get("gift_name") or "") for event in events),
        *(str(event.get("title") or "") for event in events),
    ])

    if merge_events:
        events = _merge_events(events)
    else:
        events = [{**event, "merge_count": 1} for event in events]

    font_size = 14
    line_height = int(font_size * 1.6)
    
    # 如果需要显示完整日期，将总宽度从 980 扩大至 1260
    width = 1260 if show_date else 980
    padding = 30
    gutter = 20
    column_width = (width - padding * 2 - gutter) // 2
    pages: list[str] = []
    total_pages = math.ceil(len(events) / page_size)

    time_format = "%Y-%m-%d %H:%M:%S" if show_date else "%H:%M:%S"

    def clean_text(s: str | None) -> str:
        if not s:
            return ""
        s = re.sub(r'[\u200b\uFEFF]', '', s)
        s = s.replace('\xa0', ' ')
        return s

    def layout_event(time_str: str, text: str, text_color: tuple, suffix: str, max_width: int):
        time_part = f"{time_str}  "
        try:
            indent_width = Text2Image.from_text(time_part, font_size).width
        except ValueError:
            indent_width = len(time_part) * (font_size // 2)

        lines = []
        current_line = [(time_part, text_color)]
        current_width = indent_width
        unit_width_cache: dict[str, int] = {}
        
        def add_segment(content: str, color: tuple):
            nonlocal current_line, current_width, lines
            accum = ""
            for unit in split_text_units(content):
                if unit not in unit_width_cache:
                    unit_width_cache[unit] = Text2Image.from_text(
                        unit, font_size
                    ).width
                unit_width = unit_width_cache[unit]
                if current_width + unit_width > max_width:
                    if accum:
                        current_line.append((accum, color))
                    lines.append(current_line)
                    current_line = []
                    current_width = indent_width
                    accum = unit
                    current_width += unit_width
                else:
                    accum += unit
                    current_width += unit_width
            if accum:
                current_line.append((accum, color))

        if text:
            add_segment(text, text_color)
        if suffix:
            add_segment(suffix, (160, 160, 160))
            
        if current_line:
            lines.append(current_line)
            
        return lines, indent_width

    for page_idx in range(total_pages):
        header_text = f"{title}（{page_idx + 1}/{total_pages}页）"
        header_t2i = Text2Image.from_text(header_text, 24, weight="bold", fill=(34, 34, 34))
        chunk = events[page_idx * page_size : (page_idx + 1) * page_size]
        
        left_chunk = chunk[: page_size // 2]
        right_chunk = chunk[page_size // 2 :]

        # 独立预计算左列排版和高度
        left_layouts = []
        left_height = 0
        for event in left_chunk:
            t_str = datetime.fromtimestamp(event["timestamp"]).strftime(time_format) if event.get("timestamp") else "--"
            txt, clr, sfx = _format_event_text(event)
            if show_uname:
                speaker = str(event.get("uname") or event.get("uid") or "未知用户")
                txt = f"{speaker}：{txt}"
            lines, indent_w = layout_event(t_str, clean_text(txt), clr, clean_text(sfx), column_width)
            left_height += len(lines) * line_height
            left_layouts.append((lines, indent_w))

        # 独立预计算右列排版和高度
        right_layouts = []
        right_height = 0
        for event in right_chunk:
            t_str = datetime.fromtimestamp(event["timestamp"]).strftime(time_format) if event.get("timestamp") else "--"
            txt, clr, sfx = _format_event_text(event)
            if show_uname:
                speaker = str(event.get("uname") or event.get("uid") or "未知用户")
                txt = f"{speaker}：{txt}"
            lines, indent_w = layout_event(t_str, clean_text(txt), clr, clean_text(sfx), column_width)
            right_height += len(lines) * line_height
            right_layouts.append((lines, indent_w))

        # 页面内容高度取决于较长的一列
        content_h = padding + header_t2i.height + 16 + max(left_height, right_height) + padding
        canvas = BuildImage.new("RGBA", (width, int(content_h)), (255, 255, 255, 255))
        header_t2i.draw_on_image(canvas.image, (padding, padding))

        start_y = padding + header_t2i.height + 16
        
        # 独立绘制左列
        curr_y_left = start_y
        for lines, indent_w in left_layouts:
            for line_idx, line in enumerate(lines):
                curr_x = padding + (indent_w if line_idx > 0 else 0)
                for seg_text, seg_color in line:
                    if not seg_text: continue
                    try:
                        seg_img = Text2Image.from_text(seg_text, font_size, fill=seg_color)
                        seg_img.draw_on_image(canvas.image, (curr_x, curr_y_left))
                        curr_x += seg_img.width
                    except ValueError:
                        err_img = Text2Image.from_text("[渲染失败]", font_size, fill=(255, 0, 0))
                        err_img.draw_on_image(canvas.image, (curr_x, curr_y_left))
                        curr_x += err_img.width
                curr_y_left += line_height

        # 独立绘制右列
        curr_y_right = start_y
        for lines, indent_w in right_layouts:
            for line_idx, line in enumerate(lines):
                curr_x = padding + column_width + gutter + (indent_w if line_idx > 0 else 0)
                for seg_text, seg_color in line:
                    if not seg_text: continue
                    try:
                        seg_img = Text2Image.from_text(seg_text, font_size, fill=seg_color)
                        seg_img.draw_on_image(canvas.image, (curr_x, curr_y_right))
                        curr_x += seg_img.width
                    except ValueError:
                        err_img = Text2Image.from_text("[渲染失败]", font_size, fill=(255, 0, 0))
                        err_img.draw_on_image(canvas.image, (curr_x, curr_y_right))
                        curr_x += err_img.width
                curr_y_right += line_height

        save_dir = ROOT / "data" / "images" / "events"
        save_dir.mkdir(parents=True, exist_ok=True)
        filename = f"events_{uuid.uuid4().hex[:8]}_{page_idx + 1}.png"
        save_path = save_dir / filename
        canvas.image.save(save_path)
        pages.append(str(save_path))

    return pages
