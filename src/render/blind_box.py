import uuid
from typing import Any

from nonebot_plugin_imageutils import BuildImage

from src.common.utils import ROOT, truncate_name
from src.render.emoji_text import Text2Image, prefetch_emoji_assets


RESULT_ORDER = ("电影票", "棉花糖", "爱心抱枕", "浪漫城堡")


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
                "results": {name: 0 for name in RESULT_ORDER},
            },
        )
        if row.get("uname"):
            entry["uname"] = str(row["uname"])
        entry["count"] += gift_num
        entry["results"][gift_name] += gift_num

    ranked = sorted(
        users.values(),
        key=lambda item: (-int(item["count"]), str(item["uname"]), int(item["uid"])),
    )
    for rank, item in enumerate(ranked, start=1):
        item["rank"] = rank
    return ranked


def _draw_blind_box_rank(title: str, items: list[dict[str, Any]]) -> BuildImage:
    width = 1040
    padding = 44
    header_h = 92
    row_h = 56
    footer_h = 60
    visible_items = items[:30]
    height = padding * 2 + header_h + row_h * len(visible_items) + footer_h
    canvas = BuildImage.new("RGBA", (width, height), (255, 255, 255, 255))

    Text2Image.from_text(title, 38, weight="bold", fill=(38, 38, 42)).draw_on_image(
        canvas.image, (padding, padding)
    )
    summary = f"共开出 {sum(int(item['count']) for item in items)} 盒 · {len(items)} 人参与"
    Text2Image.from_text(summary, 21, fill=(145, 145, 152)).draw_on_image(
        canvas.image, (padding, padding + 50)
    )

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
            canvas.image, (padding + 350, y)
        )

        details = "  ".join(
            f"{name}×{item['results'][name]}"
            for name in RESULT_ORDER
            if item["results"][name]
        )
        Text2Image.from_text(details, 21, fill=(115, 115, 124)).draw_on_image(
            canvas.image, (padding + 455, y + 2)
        )
        y += row_h

    if len(items) > len(visible_items):
        footer = f"仅展示前 {len(visible_items)} 名，共 {len(items)} 人"
    else:
        footer = "心动盲盒 15元/盒 · 按开盒数量排序"
    Text2Image.from_text(footer, 20, fill=(170, 170, 176)).draw_on_image(
        canvas.image, (padding, height - padding - 24)
    )
    return canvas


async def render_blind_box_rank(title: str, items: list[dict[str, Any]]) -> dict[str, str]:
    await prefetch_emoji_assets([title, *(str(item.get("uname") or "") for item in items)])
    save_dir = ROOT / "data" / "images" / "blind_box"
    save_dir.mkdir(parents=True, exist_ok=True)
    save_path = save_dir / f"blind_box_{uuid.uuid4().hex[:8]}.png"
    _draw_blind_box_rank(title, items).image.save(save_path)
    return {"image_path": str(save_path)}
