import uuid
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
from nonebot.log import logger
from nonebot_plugin_imageutils import BuildImage
from PIL import Image

from src.common.blind_box import (
    BLIND_BOX_COST_COIN,
    BLIND_BOX_GIFTS,
    BLIND_BOX_RESULT_ORDER,
)
from src.common.utils import ROOT, truncate_name
from src.render.emoji_text import Text2Image, prefetch_emoji_assets


RESULT_ORDER = BLIND_BOX_RESULT_ORDER


def _format_money(coin: int, *, signed: bool = False) -> str:
    value = coin / 1000
    prefix = "+" if signed and coin > 0 else ""
    if value.is_integer():
        return f"{prefix}¥{value:,.0f}"
    return f"{prefix}¥{value:,.1f}"


def build_blind_box_rank_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    users: dict[int, dict[str, Any]] = {}
    for row in rows:
        uid = int(row.get("uid") or 0)
        gift_name = str(row.get("gift_name") or "")
        gift_num = int(row.get("gift_num") or 0)
        if uid <= 0 or gift_name not in RESULT_ORDER or gift_num <= 0:
            continue
        entry = users.setdefault(
            uid,
            {
                "uid": uid,
                "uname": str(row.get("uname") or uid),
                "count": 0,
                "value_coin": 0,
                "results": {name: 0 for name in RESULT_ORDER},
            },
        )
        if row.get("uname"):
            entry["uname"] = str(row["uname"])
        entry["count"] += gift_num
        entry["value_coin"] += int(BLIND_BOX_GIFTS[gift_name]["coin"]) * gift_num
        entry["results"][gift_name] += gift_num

    for item in users.values():
        item["cost_coin"] = int(item["count"]) * BLIND_BOX_COST_COIN
        item["profit_coin"] = int(item["value_coin"]) - int(item["cost_coin"])

    ranked = sorted(
        users.values(),
        key=lambda item: (
            -int(item["profit_coin"]),
            -int(item["value_coin"]),
            -int(item["count"]),
            str(item["uname"]),
            int(item["uid"]),
        ),
    )
    for rank, item in enumerate(ranked, start=1):
        item["rank"] = rank
    return ranked


async def _load_gift_icons() -> dict[str, Image.Image]:
    asset_dir = ROOT / "data" / "images" / "blind_box" / "assets"
    asset_dir.mkdir(parents=True, exist_ok=True)
    icons: dict[str, Image.Image] = {}
    missing: list[tuple[str, Path, str]] = []
    for name, gift in BLIND_BOX_GIFTS.items():
        path = asset_dir / f"{name}.png"
        if path.exists():
            try:
                icons[name] = Image.open(path).convert("RGBA")
                continue
            except OSError:
                path.unlink(missing_ok=True)
        missing.append((name, path, str(gift["icon"])))

    if missing:
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://live.bilibili.com/"}
        async with httpx.AsyncClient(timeout=15, follow_redirects=True, trust_env=False) as client:
            for name, path, url in missing:
                try:
                    response = await client.get(url, headers=headers)
                    response.raise_for_status()
                    icon = Image.open(BytesIO(response.content)).convert("RGBA")
                    icon.save(path)
                    icons[name] = icon
                except Exception as exc:
                    logger.warning("下载心动盲盒礼物图标失败 name=%s: %s", name, exc)
    return icons


def _draw_blind_box_rank(
    title: str,
    items: list[dict[str, Any]],
    icons: dict[str, Image.Image] | None = None,
    *,
    all_items: list[dict[str, Any]] | None = None,
    page_index: int = 1,
    total_pages: int = 1,
) -> BuildImage:
    width = 1180
    padding = 44
    header_h = 238
    row_h = 78
    footer_h = 60
    visible_items = items
    summary_items = all_items if all_items is not None else items
    height = padding * 2 + header_h + row_h * len(visible_items) + footer_h
    canvas = BuildImage.new("RGBA", (width, height), (255, 255, 255, 255))

    Text2Image.from_text(title, 38, weight="bold", fill=(38, 38, 42)).draw_on_image(
        canvas.image, (padding, padding)
    )
    total_count = sum(int(item["count"]) for item in summary_items)
    total_cost = sum(int(item["cost_coin"]) for item in summary_items)
    total_value = sum(int(item["value_coin"]) for item in summary_items)
    total_profit = total_value - total_cost
    total_profit_rate = total_profit / total_cost * 100 if total_cost else 0
    summary = f"共开出 {total_count} 盒 · {len(summary_items)} 人参与"
    Text2Image.from_text(summary, 21, fill=(145, 145, 152)).draw_on_image(
        canvas.image, (padding, padding + 50)
    )

    metrics = (
        ("总成本", _format_money(total_cost)),
        ("总价值", _format_money(total_value)),
        (
            "盈亏",
            f"{_format_money(total_profit, signed=True)}（{total_profit_rate:+.1f}%）",
        ),
    )
    for index, (label, value) in enumerate(metrics):
        x = padding + index * 250
        Text2Image.from_text(label, 19, fill=(150, 150, 158)).draw_on_image(
            canvas.image, (x, padding + 88)
        )
        color = (210, 67, 112) if label != "盈亏" or total_profit >= 0 else (54, 145, 112)
        Text2Image.from_text(value, 28, weight="bold", fill=color).draw_on_image(
            canvas.image, (x, padding + 114)
        )

    legend_y = padding + 158
    cell_w = (width - padding * 2) // len(RESULT_ORDER)
    for index, name in enumerate(RESULT_ORDER):
        x = padding + index * cell_w
        icon = (icons or {}).get(name)
        if icon is not None:
            thumb = icon.copy()
            thumb.thumbnail((42, 42), Image.Resampling.LANCZOS)
            canvas.image.paste(thumb, (x, legend_y), thumb)
        Text2Image.from_text(name, 17, fill=(70, 70, 78)).draw_on_image(
            canvas.image, (x + 46, legend_y)
        )
        Text2Image.from_text(
            _format_money(int(BLIND_BOX_GIFTS[name]["coin"])),
            16,
            fill=(155, 155, 164),
        ).draw_on_image(canvas.image, (x + 46, legend_y + 23))

    y = padding + header_h
    for item in visible_items:
        rank = int(item["rank"])
        uname = truncate_name(str(item["uname"]), max_len=22)
        count = int(item["count"])
        rank_color = (212, 80, 125) if rank <= 3 else (92, 92, 100)
        Text2Image.from_text(str(rank), 25, weight="bold", fill=rank_color).draw_on_image(
            canvas.image, (padding, y)
        )
        Text2Image.from_text(uname, 25, fill=(48, 48, 54)).draw_on_image(
            canvas.image, (padding + 52, y)
        )
        Text2Image.from_text(f"{count}盒", 24, weight="bold", fill=(195, 65, 112)).draw_on_image(
            canvas.image, (padding + 330, y)
        )

        cost_coin = int(item["cost_coin"])
        value_coin = int(item["value_coin"])
        profit_coin = int(item["profit_coin"])
        financials = (
            f"成本 {_format_money(cost_coin)}",
            f"价值 {_format_money(value_coin)}",
            f"盈亏 {_format_money(profit_coin, signed=True)}",
        )
        for index, value in enumerate(financials):
            color = (84, 84, 92)
            if index == 2:
                color = (205, 65, 108) if profit_coin >= 0 else (48, 140, 108)
            Text2Image.from_text(value, 20, fill=color).draw_on_image(
                canvas.image, (padding + 440 + index * 205, y + 2)
            )

        detail_x = padding + 52
        for name in RESULT_ORDER:
            result_count = int(item["results"][name])
            if not result_count:
                continue
            icon = (icons or {}).get(name)
            if icon is not None:
                thumb = icon.copy()
                thumb.thumbnail((28, 28), Image.Resampling.LANCZOS)
                canvas.image.paste(thumb, (detail_x, y + 36), thumb)
                detail_x += 31
            else:
                Text2Image.from_text(name, 17, fill=(135, 135, 144)).draw_on_image(
                    canvas.image, (detail_x, y + 39)
                )
                detail_x += 74
            count_text = f"×{result_count}"
            Text2Image.from_text(count_text, 17, fill=(135, 135, 144)).draw_on_image(
                canvas.image, (detail_x, y + 39)
            )
            detail_x += 42 + max(0, len(str(result_count)) - 1) * 10
        y += row_h

    page_label = f"第 {page_index}/{total_pages} 页 · " if total_pages > 1 else ""
    footer = (
        f"{page_label}数据自2026-04-11 · 价值按官方标价计算 "
        "· ¥15/盒 · 按盈亏排序"
    )
    Text2Image.from_text(footer, 20, fill=(170, 170, 176)).draw_on_image(
        canvas.image, (padding, height - padding - 24)
    )
    return canvas


async def render_blind_box_rank(
    title: str,
    items: list[dict[str, Any]],
    page_size: int = 15,
) -> list[dict[str, str]]:
    await prefetch_emoji_assets([title, *(str(item.get("uname") or "") for item in items)])
    icons = await _load_gift_icons()
    save_dir = ROOT / "data" / "images" / "blind_box"
    save_dir.mkdir(parents=True, exist_ok=True)
    page_size = max(1, page_size)
    total_pages = max(1, (len(items) + page_size - 1) // page_size)
    render_id = uuid.uuid4().hex[:8]
    results: list[dict[str, str]] = []
    for page_index in range(total_pages):
        page_items = items[page_index * page_size : (page_index + 1) * page_size]
        save_path = save_dir / f"blind_box_{render_id}_{page_index + 1}.png"
        _draw_blind_box_rank(
            title,
            page_items,
            icons,
            all_items=items,
            page_index=page_index + 1,
            total_pages=total_pages,
        ).image.save(save_path)
        results.append({"image_path": str(save_path)})
    return results
