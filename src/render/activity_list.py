from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from nonebot_plugin_imageutils import BuildImage

from src.common.text import split_text_units
from src.common.utils import ROOT
from src.render.activity import extract_dynamic_info
from src.render.emoji_text import Text2Image, prefetch_emoji_assets


PAGE_SIZE = 35


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _item_summary(item: dict[str, Any]) -> str:
    info = extract_dynamic_info(item)
    parts: list[str] = []
    text = _normalize_text(str(info.get("text") or ""))
    if text:
        parts.append(text)
    if info.get("pic_urls"):
        parts.append("[图片]")

    orig = item.get("orig")
    if isinstance(orig, dict):
        origin = extract_dynamic_info(orig)
        origin_parts: list[str] = []
        origin_text = _normalize_text(str(origin.get("text") or ""))
        if origin_text:
            origin_parts.append(origin_text)
        if origin.get("pic_urls"):
            origin_parts.append("[图片]")
        if origin_parts:
            parts.append("// " + " ".join(origin_parts))

    return " ".join(parts) or "（无文字内容）"


def _ellipsize(text: str, font_size: int, max_width: int) -> str:
    if Text2Image.from_text(text, font_size).width <= max_width:
        return text

    units = split_text_units(text)
    suffix = "…"
    low, high = 0, len(units)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = "".join(units[:middle]) + suffix
        if Text2Image.from_text(candidate, font_size).width <= max_width:
            low = middle
        else:
            high = middle - 1
    return "".join(units[:low]).rstrip() + suffix


async def render_activity_list(
    room_id: int,
    year: int,
    month: int,
    activities: list[dict[str, Any]],
) -> list[Path]:
    rows: list[dict[str, str | int]] = []
    day_counts: defaultdict[str, int] = defaultdict(int)
    for activity in activities:
        published_at = datetime.fromtimestamp(int(activity["timestamp"]))
        date_key = published_at.strftime("%Y-%m-%d")
        day_counts[date_key] += 1
        rows.append(
            {
                "date": published_at.strftime("%m-%d"),
                "time": published_at.strftime("%H:%M"),
                "index": day_counts[date_key],
                "summary": _item_summary(activity["item"]),
            }
        )

    await prefetch_emoji_assets(
        [str(row["summary"]) for row in rows]
    )

    save_dir = ROOT / "data" / "images" / "activity_list"
    save_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    total_pages = (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE

    width = 1100
    padding = 40
    row_height = 50
    title_height = 82
    summary_x = 275
    summary_width = width - padding - summary_x

    for page_index in range(total_pages):
        page_rows = rows[
            page_index * PAGE_SIZE : (page_index + 1) * PAGE_SIZE
        ]
        height = padding * 2 + title_height + len(page_rows) * row_height
        canvas = BuildImage.new("RGBA", (width, height), (255, 255, 255, 255))

        title = Text2Image.from_text(
            f"{year:04d} 年 {month:02d} 月动态",
            36,
            weight="bold",
            fill=(34, 34, 34),
        )
        title.draw_on_image(canvas.image, (padding, padding))
        page_label = (
            f"共 {len(rows)} 条 · 第 {page_index + 1}/{total_pages} 页"
        )
        page_text = Text2Image.from_text(
            page_label,
            21,
            fill=(145, 145, 145),
        )
        page_text.draw_on_image(
            canvas.image,
            (width - padding - page_text.width, padding + 10),
        )

        y = padding + title_height
        for row in page_rows:
            prefix = (
                f"{row['date']}  {row['time']}  #{row['index']}"
            )
            Text2Image.from_text(
                prefix,
                24,
                fill=(105, 105, 105),
            ).draw_on_image(canvas.image, (padding, y))

            summary = _ellipsize(
                str(row["summary"]),
                24,
                summary_width,
            )
            Text2Image.from_text(
                summary,
                24,
                fill=(34, 34, 34),
            ).draw_on_image(canvas.image, (summary_x, y))
            y += row_height

        out_path = save_dir / (
            f"{room_id}_{year:04d}{month:02d}_{page_index + 1}.png"
        )
        canvas.image.convert("RGB").save(out_path, format="PNG")
        paths.append(out_path)

    return paths
