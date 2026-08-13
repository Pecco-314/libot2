from __future__ import annotations

from pathlib import Path
from typing import Any

from nonebot_plugin_imageutils import BuildImage

from src.common.text import split_text_units
from src.common.utils import ROOT
from src.render.emoji_text import Text2Image


ROWS_PER_COLUMN = 75
COLUMN_COUNT = 8
PAGE_SIZE = ROWS_PER_COLUMN * COLUMN_COUNT
FONT_PATH = str(ROOT / "fonts" / "NotoSansCJKsc-Regular.otf")

GUARD_LABELS = {
    1: "总",
    2: "提",
    3: "舰",
}


def _level_color(level: int) -> tuple[int, int, int]:
    if level > 50:
        return (139, 25, 38)  # 深红
    if level > 40:
        return (91, 44, 131)  # 深紫
    if level > 30:
        return (35, 71, 142)  # 深蓝
    if level > 20:
        return (0, 105, 112)  # 深青
    if level > 10:
        return (173, 45, 91)  # 深粉
    return (82, 82, 82)  # 深灰


def _text(text: str, font_size: int, **kwargs: Any) -> Text2Image:
    return Text2Image.from_text(
        text,
        font_size,
        fontname=FONT_PATH,
        **kwargs,
    )


def _ellipsize(text: str, font_size: int, max_width: int) -> str:
    if _text(text, font_size).width <= max_width:
        return text
    units = split_text_units(text)
    low, high = 0, len(units)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = "".join(units[:middle]) + "…"
        if _text(candidate, font_size).width <= max_width:
            low = middle
        else:
            high = middle - 1
    return "".join(units[:low]).rstrip() + "…"


def render_fan_club_members(
    snapshot: dict[str, Any],
    members: list[dict[str, Any]],
) -> list[Path]:
    total_pages = max(1, (len(members) + PAGE_SIZE - 1) // PAGE_SIZE)
    save_dir = (
        ROOT
        / "data"
        / "images"
        / "fan_club"
        / str(int(snapshot["id"]))
    )
    save_dir.mkdir(parents=True, exist_ok=True)
    paths = [save_dir / f"{page:03d}.png" for page in range(1, total_pages + 1)]
    if all(path.exists() for path in paths):
        return paths

    width = 2048
    padding_x = 32
    padding_y = 34
    header_height = 76
    row_height = 19
    font_size = 15
    column_gap = 12
    column_width = (
        width - padding_x * 2 - column_gap * (COLUMN_COUNT - 1)
    ) // COLUMN_COUNT
    height = padding_y * 2 + header_height + ROWS_PER_COLUMN * row_height

    for page_index, out_path in enumerate(paths):
        page_members = members[
            page_index * PAGE_SIZE : (page_index + 1) * PAGE_SIZE
        ]
        canvas = BuildImage.new("RGBA", (width, height), (255, 255, 255, 255))

        title = _text(
            f"{snapshot['short_name']}粉丝团成员",
            30,
            weight="bold",
            fill=(34, 34, 34),
        )
        title.draw_on_image(canvas.image, (padding_x, padding_y))
        meta = _text(
            f"{snapshot['snapshot_date']} · {len(members)} 人 · "
            f"{page_index + 1}/{total_pages}",
            18,
            fill=(130, 130, 130),
        )
        meta.draw_on_image(
            canvas.image,
            (width - padding_x - meta.width, padding_y + 8),
        )

        for item_index, member in enumerate(page_members):
            column = item_index // ROWS_PER_COLUMN
            row = item_index % ROWS_PER_COLUMN
            x = padding_x + column * (column_width + column_gap)
            y = padding_y + header_height + row * row_height
            level = int(member["level"])
            guard_label = GUARD_LABELS.get(int(member.get("guard_level") or 0), "")
            level_text = f"{level:>2}{guard_label} "
            level_image = _text(
                level_text,
                font_size,
                fill=_level_color(level),
            )
            level_image.draw_on_image(canvas.image, (x, y))
            name_width = column_width - level_image.width
            uname = _ellipsize(str(member["uname"]), font_size, name_width)
            _text(
                uname,
                font_size,
                fill=(48, 48, 48),
            ).draw_on_image(canvas.image, (x + level_image.width, y))

        canvas.image.convert("RGB").save(out_path, format="PNG", optimize=True)

    return paths
