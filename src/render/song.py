import uuid
from datetime import date, datetime
from typing import Any

from PIL import ImageDraw
from nonebot_plugin_imageutils import BuildImage

from src.common.utils import ROOT
from src.db.song_list import (
    get_songs_of_date,
    list_recent_song_records_by_singer,
    list_songs_by_singer,
    random_song,
    search_songs_by_title,
)
from src.render.emoji_text import Text2Image, prefetch_emoji_assets


BACKGROUND = (255, 255, 255, 255)
TEXT = (38, 38, 42)
MUTED = (112, 112, 122)
FAINT = (172, 172, 180)
ACCENT = (105, 89, 205)
SOFT = (247, 246, 252, 255)
SOURCE_TEXT = "数据来源于三理Mit3uri的歌单（mit3uri.live），感谢作者。"


def _text(
    value: str,
    size: int,
    *,
    weight: str = "normal",
    fill: tuple[int, int, int] = TEXT,
) -> Text2Image:
    return Text2Image.from_text(value, size, weight=weight, fill=fill)


def _wrapped(
    value: str,
    size: int,
    max_width: int,
    *,
    weight: str = "normal",
    fill: tuple[int, int, int] = TEXT,
) -> Text2Image:
    wrapped = _text(value, size, weight=weight).wrap(max_width).wrapped_text
    return _text(wrapped, size, weight=weight, fill=fill)


def _relative_time(date_str: str) -> str:
    try:
        record_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return date_str
    delta = (datetime.now().date() - record_date).days
    if delta == 0:
        return "今天"
    if delta == 1:
        return "昨天"
    if 1 < delta < 7:
        return f"{delta}天前"
    if delta < 30:
        return f"{delta // 7}周前"
    if delta < 365:
        return f"{delta // 30}个月前"
    return f"{delta // 365}年前"


def _column_count(item_count: int) -> int:
    if item_count > 48:
        return 3
    if item_count > 18:
        return 2
    return 1


def _partition_by_height(
    items: list[Text2Image],
    columns: int,
    gap: int,
) -> list[list[Text2Image]]:
    if columns <= 1 or len(items) <= 1:
        return [items]
    costs = [item.height + gap for item in items]
    groups: list[list[Text2Image]] = []
    start = 0
    remaining_height = sum(costs)
    for group_index in range(columns - 1):
        remaining_groups = columns - group_index
        target = remaining_height / remaining_groups
        end = start
        height = 0
        max_end = len(items) - (remaining_groups - 1)
        while end < max_end:
            next_height = height + costs[end]
            if height and abs(target - height) <= abs(target - next_height):
                break
            height = next_height
            end += 1
        if end == start:
            end += 1
            height = costs[start]
        groups.append(items[start:end])
        start = end
        remaining_height -= height
    groups.append(items[start:])
    return groups


def _draw_footer(canvas: BuildImage, width: int, padding: int, y: int) -> None:
    footer = _text(SOURCE_TEXT, 17, fill=FAINT)
    footer.draw_on_image(canvas.image, (width - padding - footer.width, y))


def draw_song_card(song: dict[str, Any]) -> BuildImage:
    width = 720
    padding = 44
    content_width = width - padding * 2

    records: list[date] = []
    for value in song.get("records") or []:
        try:
            records.append(datetime.strptime(str(value), "%Y-%m-%d").date())
        except ValueError:
            continue
    records.sort(reverse=True)
    recent_dates = records[:5]

    label = _text("歌曲", 18, weight="bold", fill=ACCENT)
    title = _wrapped(str(song["title"]), 42, content_width, weight="bold")
    singer = _wrapped(
        f"原唱  {song.get('original_singer') or '未知'}",
        24,
        content_width,
        fill=MUTED,
    )
    count = _text(f"已演唱 {int(song.get('count') or 0)} 次", 28, weight="bold", fill=ACCENT)
    recent_label = _text("最近演唱", 23, weight="bold")
    date_items = [
        _text(
            f"{value:%Y-%m-%d}  ·  {_relative_time(value.strftime('%Y-%m-%d'))}",
            22,
            fill=MUTED,
        )
        for value in recent_dates
    ]
    if not date_items:
        date_items = [_text("暂无日期记录", 22, fill=FAINT)]

    height = (
        padding
        + label.height
        + 12
        + title.height
        + 12
        + singer.height
        + 30
        + count.height
        + 34
        + recent_label.height
        + 14
        + sum(item.height + 11 for item in date_items)
        + 31
        + 20
        + padding
    )
    canvas = BuildImage.new("RGBA", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(canvas.image)
    draw.rounded_rectangle(
        (padding - 14, padding - 12, padding - 7, padding + label.height + title.height + 28),
        radius=4,
        fill=ACCENT + (255,),
    )

    y = padding
    label.draw_on_image(canvas.image, (padding, y))
    y += label.height + 12
    title.draw_on_image(canvas.image, (padding, y))
    y += title.height + 12
    singer.draw_on_image(canvas.image, (padding, y))
    y += singer.height + 30
    count.draw_on_image(canvas.image, (padding, y))
    y += count.height + 34
    recent_label.draw_on_image(canvas.image, (padding, y))
    y += recent_label.height + 14
    for item in date_items:
        item.draw_on_image(canvas.image, (padding, y))
        y += item.height + 11
    y += 20
    _draw_footer(canvas, width, padding, y)
    return canvas


def save_song_card(song: dict[str, Any]) -> dict[str, Any]:
    save_dir = ROOT / "data" / "images" / "song_list"
    save_dir.mkdir(parents=True, exist_ok=True)
    canvas = draw_song_card(song)
    save_path = save_dir / f"song_{song['id']}_{uuid.uuid4().hex[:6]}.png"
    canvas.image.convert("RGB").save(save_path, format="PNG", optimize=True)
    return {"image_path": str(save_path), "data": song}


async def render_songs_by_keyword(
    keyword: str,
    limit: int = 5,
) -> list[dict[str, Any]]:
    songs = search_songs_by_title(keyword, limit)
    await prefetch_emoji_assets(
        [
            keyword,
            *(str(song.get("title") or "") for song in songs),
            *(str(song.get("original_singer") or "") for song in songs),
        ]
    )
    return [save_song_card(song) for song in songs]


async def render_random_song(limit: int = 3) -> dict[str, Any] | None:
    song = random_song(limit)
    if song is None:
        return None
    await prefetch_emoji_assets(
        [str(song.get("title") or ""), str(song.get("original_singer") or "")]
    )
    return save_song_card(song)


def _draw_singer_song_list(
    singer: str,
    songs: list[dict[str, Any]],
    recent_records: list[dict[str, Any]],
) -> BuildImage:
    columns = _column_count(len(songs))
    padding = 44
    sidebar_width = 310
    column_width = 370 if columns > 1 else 480
    section_gap = 38
    column_gap = 30
    width = (
        padding * 2
        + sidebar_width
        + section_gap
        + columns * column_width
        + (columns - 1) * column_gap
    )

    label = _text("歌手", 18, weight="bold", fill=ACCENT)
    title = _wrapped(singer, 40, width - padding * 2, weight="bold")
    total_performances = sum(int(song.get("count") or 0) for song in songs)
    meta = _text(
        f"共 {len(songs)} 首歌  ·  演唱 {total_performances} 次",
        23,
        fill=MUTED,
    )
    recent_label = _text("最近演唱", 24, weight="bold")
    recent_items: list[tuple[Text2Image, Text2Image]] = []
    for record in recent_records:
        date_value = str(record["record_date"])
        date_line = _text(
            f"{date_value}  ·  {_relative_time(date_value)}",
            16,
            fill=MUTED,
        )
        song_line = _wrapped(
            str(record["title"]),
            20,
            sidebar_width - 40,
            weight="bold",
        )
        recent_items.append((date_line, song_line))

    all_label = _text("全部歌曲", 24, weight="bold")
    song_items = [
        _wrapped(
            f"{index:02d}  {song['title']}  ·  {int(song.get('count') or 0)}次",
            21,
            column_width,
        )
        for index, song in enumerate(songs, start=1)
    ]
    song_columns = _partition_by_height(song_items, columns, 13)
    song_columns_height = max(
        sum(item.height + 13 for item in column) for column in song_columns
    )
    recent_height = recent_label.height + 22
    for date_line, song_line in recent_items:
        recent_height += date_line.height + 5 + song_line.height + 19
    main_height = all_label.height + 22 + song_columns_height

    header_height = label.height + 10 + title.height + 11 + meta.height
    body_height = max(recent_height + 32, main_height)
    footer_height = 20
    height = padding + header_height + 34 + body_height + 30 + footer_height + padding
    canvas = BuildImage.new("RGBA", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(canvas.image)

    y = padding
    label.draw_on_image(canvas.image, (padding, y))
    y += label.height + 10
    title.draw_on_image(canvas.image, (padding, y))
    y += title.height + 11
    meta.draw_on_image(canvas.image, (padding, y))
    body_y = y + meta.height + 34

    sidebar_bottom = body_y + recent_height + 26
    draw.rounded_rectangle(
        (padding, body_y, padding + sidebar_width, sidebar_bottom),
        radius=18,
        fill=SOFT,
    )
    side_x = padding + 20
    side_y = body_y + 20
    recent_label.draw_on_image(canvas.image, (side_x, side_y))
    side_y += recent_label.height + 22
    for date_line, song_line in recent_items:
        date_line.draw_on_image(canvas.image, (side_x, side_y))
        side_y += date_line.height + 5
        song_line.draw_on_image(canvas.image, (side_x, side_y))
        side_y += song_line.height + 19

    main_x = padding + sidebar_width + section_gap
    all_label.draw_on_image(canvas.image, (main_x, body_y))
    list_y = body_y + all_label.height + 22
    for column_index, column in enumerate(song_columns):
        x = main_x + column_index * (column_width + column_gap)
        item_y = list_y
        for item in column:
            item.draw_on_image(canvas.image, (x, item_y))
            item_y += item.height + 13

    _draw_footer(canvas, width, padding, height - padding - footer_height)
    return canvas


def _save_singer_song_list(
    singer: str,
    songs: list[dict[str, Any]],
    recent_records: list[dict[str, Any]],
) -> str:
    save_dir = ROOT / "data" / "images" / "song_list"
    save_dir.mkdir(parents=True, exist_ok=True)
    canvas = _draw_singer_song_list(singer, songs, recent_records)
    save_path = save_dir / f"singer_{uuid.uuid4().hex[:8]}.png"
    canvas.image.convert("RGB").save(save_path, format="PNG", optimize=True)
    return str(save_path)


async def render_songs_by_singer(singer: str) -> dict[str, Any] | None:
    songs = list_songs_by_singer(singer)
    if not songs:
        return None
    recent_records = list_recent_song_records_by_singer(singer, limit=5)
    await prefetch_emoji_assets(
        [
            singer,
            *(str(song.get("title") or "") for song in songs),
            *(str(record.get("title") or "") for record in recent_records),
        ]
    )
    image_path = _save_singer_song_list(singer, songs, recent_records)
    return {
        "image_path": image_path,
        "data": {
            "singer": singer,
            "count": len(songs),
            "recent_records": recent_records,
        },
    }


def _draw_song_list_by_date(
    target_date: date,
    songs: list[dict[str, Any]],
) -> BuildImage:
    columns = _column_count(len(songs))
    padding = 44
    column_width = 430
    column_gap = 36
    width = padding * 2 + columns * column_width + (columns - 1) * column_gap

    label = _text("歌单", 18, weight="bold", fill=ACCENT)
    title = _text(target_date.strftime("%Y-%m-%d"), 40, weight="bold")
    meta = _text(f"共 {len(songs)} 首", 23, fill=MUTED)
    items = [
        _wrapped(
            (
                f"{index:02d}  {song['title']} — "
                f"{song.get('original_singer') or '未知'}  ·  累计{int(song.get('count') or 0)}次"
            ),
            21,
            column_width,
        )
        for index, song in enumerate(songs, start=1)
    ]
    item_columns = _partition_by_height(items, columns, 14)
    list_height = max(
        sum(item.height + 14 for item in column) for column in item_columns
    )
    header_height = label.height + 10 + title.height + 11 + meta.height
    footer_height = 20
    height = padding + header_height + 34 + list_height + 28 + footer_height + padding
    canvas = BuildImage.new("RGBA", (width, height), BACKGROUND)

    y = padding
    label.draw_on_image(canvas.image, (padding, y))
    y += label.height + 10
    title.draw_on_image(canvas.image, (padding, y))
    y += title.height + 11
    meta.draw_on_image(canvas.image, (padding, y))
    list_y = y + meta.height + 34
    for column_index, column in enumerate(item_columns):
        x = padding + column_index * (column_width + column_gap)
        item_y = list_y
        for item in column:
            item.draw_on_image(canvas.image, (x, item_y))
            item_y += item.height + 14

    _draw_footer(canvas, width, padding, height - padding - footer_height)
    return canvas


def _save_song_list_by_date(target_date: date, songs: list[dict[str, Any]]) -> str:
    save_dir = ROOT / "data" / "images" / "song_list"
    save_dir.mkdir(parents=True, exist_ok=True)
    canvas = _draw_song_list_by_date(target_date, songs)
    save_path = (
        save_dir
        / f"song_list_{target_date.strftime('%Y%m%d')}_{uuid.uuid4().hex[:6]}.png"
    )
    canvas.image.convert("RGB").save(save_path, format="PNG", optimize=True)
    return str(save_path)


async def render_songs_by_date(target_date: date) -> dict[str, Any] | None:
    songs = get_songs_of_date(target_date)
    if not songs:
        return None
    await prefetch_emoji_assets(
        [
            *(str(song.get("title") or "") for song in songs),
            *(str(song.get("original_singer") or "") for song in songs),
        ]
    )
    image_path = _save_song_list_by_date(target_date, songs)
    return {
        "image_path": image_path,
        "data": {
            "date": target_date.strftime("%Y-%m-%d"),
            "count": len(songs),
        },
    }
