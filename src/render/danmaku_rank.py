import uuid
from typing import Any

from nonebot_plugin_imageutils import BuildImage
from src.render.emoji_text import Text2Image, prefetch_emoji_assets

from src.common.utils import ROOT, truncate_name
from src.common.danmaku_rank import _count_special_tags, build_danmaku_rank_items


def draw_danmaku_rank(title: str, items: list[dict[str, Any]]) -> BuildImage:
    width = 760
    padding = 40
    content_width = width - padding * 2
    bg_color = (255, 255, 255, 255)

    title_t2i = Text2Image.from_text(title, 36, weight="bold", fill=(34, 34, 34)).wrap(content_width)

    row_items: list[dict[str, Any]] = []
    for entry in items:
        rank = entry["rank"]
        uname = truncate_name(entry["uname"], max_len=24)
        count = entry["count"]
        dc = entry.get("dc", 0)
        gn = entry.get("gn", 0)
        ky = entry.get("ky", 0)

        main_text = f"{rank}. {uname}  {count}条"
        main_img = Text2Image.from_text(main_text, 24, fill=(50, 50, 50))

        suffix_text = f"（打call {dc}条，晚安{gn}条，看你 {ky}条）"
        suffix_img = Text2Image.from_text(suffix_text, 20, fill=(160, 160, 160))

        row_items.append({
            "main": main_img,
            "suffix": suffix_img,
        })

    content_h = (
        padding +
        title_t2i.height + 16 +
        sum(
            max(item["main"].height, item["suffix"].height) + 10
            if item["main"].width + 8 + item["suffix"].width <= content_width
            else item["main"].height + item["suffix"].height + 12
            for item in row_items
        ) +
        padding
    )

    canvas = BuildImage.new("RGBA", (width, int(content_h)), bg_color)

    curr_y = padding
    title_t2i.draw_on_image(canvas.image, (padding, curr_y))
    curr_y += title_t2i.height + 16

    for item in row_items:
        main_img = item["main"]
        suffix_img = item["suffix"]
        if main_img.width + 8 + suffix_img.width <= content_width:
            main_img.draw_on_image(canvas.image, (padding, curr_y))
            suffix_img.draw_on_image(canvas.image, (padding + main_img.width + 8, curr_y + 2))
            curr_y += max(main_img.height, suffix_img.height) + 10
        else:
            main_img.draw_on_image(canvas.image, (padding, curr_y))
            curr_y += main_img.height + 4
            suffix_img.draw_on_image(canvas.image, (padding + 12, curr_y))
            curr_y += suffix_img.height + 8

    return canvas


def save_danmaku_rank(title: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    save_dir = ROOT / "data" / "images" / "rank"
    save_dir.mkdir(parents=True, exist_ok=True)

    canvas = draw_danmaku_rank(title, items)
    file_name = f"danmaku_rank_{uuid.uuid4().hex[:8]}.png"
    save_path = save_dir / file_name
    canvas.image.save(save_path)

    return {"image_path": str(save_path)}


async def render_danmaku_rank(title: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    await prefetch_emoji_assets([
        title,
        *(str(item.get("uname") or "") for item in items),
    ])
    return save_danmaku_rank(title, items)
