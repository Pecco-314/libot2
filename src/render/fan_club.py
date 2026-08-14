from __future__ import annotations

from pathlib import Path
from typing import Any

from nonebot_plugin_imageutils import BuildImage
from PIL import ImageDraw

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

COMMON_ROWS_PER_BLOCK = 75


def _common_block_count(target_count: int) -> int:
    if target_count == 2:
        return 4
    if target_count == 3:
        return 3
    return 2


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


def _level_text(member: dict[str, Any]) -> str:
    level = int(member["level"])
    guard_label = GUARD_LABELS.get(int(member.get("guard_level") or 0), "")
    return f"{level}{guard_label}"


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
            level_text = f"{_level_text(member):>3} "
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


def render_common_fan_club_members(
    snapshots: list[dict[str, Any]],
    members: list[dict[str, Any]],
) -> list[Path]:
    if not 2 <= len(snapshots) <= 5:
        raise ValueError("common fan-club rendering requires 2 to 5 targets")

    block_count = _common_block_count(len(snapshots))
    page_size = COMMON_ROWS_PER_BLOCK * block_count
    total_pages = max(1, (len(members) + page_size - 1) // page_size)
    cache_key = "-".join(str(int(snapshot["id"])) for snapshot in snapshots)
    save_dir = ROOT / "data" / "images" / "fan_club" / "common-v3" / cache_key
    save_dir.mkdir(parents=True, exist_ok=True)
    paths = [save_dir / f"{page:03d}.png" for page in range(1, total_pages + 1)]
    if all(path.exists() for path in paths):
        return paths

    width = 2048
    padding_x = 34
    padding_y = 32
    header_height = 82
    table_header_height = 30
    row_height = 20
    block_gap = 18 if block_count == 4 else 24
    block_width = (
        width - padding_x * 2 - block_gap * (block_count - 1)
    ) // block_count
    name_width = {2: 245, 3: 255}.get(len(snapshots), 280)
    level_width = (block_width - name_width) // len(snapshots)
    short_names = [str(snapshot["short_name"]) for snapshot in snapshots]
    title_text = "、".join(short_names) + "的共同粉丝团"

    for page_index, out_path in enumerate(paths):
        page_members = members[
            page_index * page_size : (page_index + 1) * page_size
        ]
        rows_in_block = max(
            1,
            (len(page_members) + block_count - 1) // block_count,
        )
        height = (
            padding_y * 2
            + header_height
            + table_header_height
            + rows_in_block * row_height
        )
        canvas = BuildImage.new("RGBA", (width, height), (255, 255, 255, 255))
        draw = ImageDraw.Draw(canvas.image)

        title_value = _ellipsize(title_text, 30, width - padding_x * 2 - 360)
        title = _text(title_value, 30, weight="bold", fill=(34, 34, 34))
        title.draw_on_image(canvas.image, (padding_x, padding_y))
        meta = _text(
            f"{snapshots[0]['snapshot_date']} · {len(members)} 人 · "
            f"{page_index + 1}/{total_pages}",
            18,
            fill=(130, 130, 130),
        )
        meta.draw_on_image(
            canvas.image,
            (width - padding_x - meta.width, padding_y + 8),
        )

        table_y = padding_y + header_height
        for block in range(block_count):
            block_x = padding_x + block * (block_width + block_gap)
            draw.rounded_rectangle(
                (
                    block_x,
                    table_y,
                    block_x + block_width,
                    table_y + table_header_height,
                ),
                radius=5,
                fill=(244, 244, 247, 255),
            )
            _text("用户名", 14, weight="bold", fill=(76, 76, 82)).draw_on_image(
                canvas.image,
                (block_x + 8, table_y + 5),
            )

            for target_index, short_name in enumerate(short_names):
                cell_x = block_x + name_width + target_index * level_width
                header = _ellipsize(short_name, 14, level_width - 8)
                header_image = _text(
                    header,
                    14,
                    weight="bold",
                    fill=(76, 76, 82),
                )
                header_image.draw_on_image(
                    canvas.image,
                    (
                        cell_x + max(0, (level_width - header_image.width) // 2),
                        table_y + 5,
                    ),
                )

            block_members = page_members[
                block * rows_in_block : (block + 1) * rows_in_block
            ]
            for row, member in enumerate(block_members):
                y = table_y + table_header_height + row * row_height
                if row % 2:
                    draw.rectangle(
                        (block_x, y, block_x + block_width, y + row_height),
                        fill=(250, 250, 252, 255),
                    )
                uname = _ellipsize(str(member["uname"]), 14, name_width - 14)
                _text(uname, 14, fill=(48, 48, 48)).draw_on_image(
                    canvas.image,
                    (block_x + 7, y + 2),
                )

                memberships = member["memberships"]
                for target_index, membership in enumerate(memberships):
                    cell_x = block_x + name_width + target_index * level_width
                    value = _level_text(membership)
                    value_image = _text(
                        value,
                        14,
                        weight="bold",
                        fill=_level_color(int(membership["level"])),
                    )
                    value_image.draw_on_image(
                        canvas.image,
                        (
                            cell_x + max(0, (level_width - value_image.width) // 2),
                            y + 2,
                        ),
                    )

            for target_index in range(len(snapshots) + 1):
                line_x = (
                    block_x + name_width + target_index * level_width
                    if target_index
                    else block_x + name_width
                )
                if target_index < len(snapshots):
                    draw.line(
                        (line_x, table_y, line_x, height - padding_y),
                        fill=(231, 231, 235, 255),
                        width=1,
                    )

        canvas.image.convert("RGB").save(out_path, format="PNG", optimize=True)

    return paths
