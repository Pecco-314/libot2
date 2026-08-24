import uuid
from collections import Counter
from datetime import date, datetime
from typing import Any

from PIL import ImageDraw
from nonebot_plugin_imageutils import BuildImage

from src.common.utils import ROOT
from src.db.song_list import (
    get_songs_of_date,
    list_song_catalog,
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
    footer_height = 0
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
    footer_height = 0
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


SONG_LIST_MODES = {"按顺序", "按歌手", "按语言", "按歌名", "按次数"}
SLASHED_SINGER_NAMES = ("K/DA", "HUNTR/X")


def _song_sort_text(value: Any) -> tuple[str, str]:
    text = str(value or "")
    return (text.casefold(), text)


def _split_singers(value: Any) -> list[str]:
    text = str(value or "未知")
    protected: dict[str, str] = {}
    for index, singer in enumerate(SLASHED_SINGER_NAMES):
        token = f"\x00{index}\x00"
        if singer in text:
            text = text.replace(singer, token)
            protected[token] = singer
    return list(
        dict.fromkeys(
            protected.get(part.strip(), part.strip())
            for part in text.split("/")
            if part.strip()
        )
    ) or ["未知"]


def _count_singer_appearances(songs: list[dict[str, Any]]) -> Counter[str]:
    unique_songs = {int(song["id"]): song for song in songs}
    return Counter(
        singer
        for song in unique_songs.values()
        for singer in _split_singers(song.get("original_singer"))
    )


def _prepare_song_list_groups(
    songs: list[dict[str, Any]],
    mode: str,
    singer_appearances: Counter[str] | None = None,
) -> list[tuple[str | None, list[dict[str, Any]]]]:
    if mode == "按顺序":
        ordered = sorted(
            songs,
            key=lambda song: int(song.get("record_id") or 0),
        )
        return [(None, ordered)]
    if mode == "按次数":
        ordered = sorted(
            songs,
            key=lambda song: (
                -int(song.get("count") or 0),
                _song_sort_text(song.get("title")),
                int(song.get("record_id") or 0),
            ),
        )
        return [(None, ordered)]
    if mode == "按歌名":
        ordered = sorted(
            songs,
            key=lambda song: (
                _song_sort_text(song.get("title")),
                -int(song.get("count") or 0),
                int(song.get("record_id") or 0),
            ),
        )
        return [(None, ordered)]

    if mode == "按歌手" and singer_appearances is None:
        singer_appearances = _count_singer_appearances(songs)

    grouped: dict[str, list[dict[str, Any]]] = {}
    for song in songs:
        if mode == "按歌手":
            singers = _split_singers(song.get("original_singer"))
            appearances = singer_appearances or Counter()
            group_name = max(singers, key=lambda singer: appearances[singer])
        else:
            group_name = str(song.get("language") or "未知").strip() or "未知"
        grouped.setdefault(group_name, []).append(song)

    language_order = {
        "华语": 0,
        "粤语": 1,
        "英语": 2,
        "日语": 3,
        "韩语": 4,
        "多语种": 5,
        "未知": 99,
    }

    def group_key(item: tuple[str, list[dict[str, Any]]]) -> tuple[Any, ...]:
        name, _ = item
        if mode == "按语言":
            return (language_order.get(name, 20), _song_sort_text(name))
        return _song_sort_text(name)

    result: list[tuple[str | None, list[dict[str, Any]]]] = []
    for name, group_songs in sorted(grouped.items(), key=group_key):
        group_songs.sort(
            key=lambda song: (
                -int(song.get("count") or 0),
                _song_sort_text(song.get("title")),
                int(song.get("record_id") or 0),
            )
        )
        result.append((name, group_songs))
    return result


def _compact_song_line(song: dict[str, Any], mode: str) -> str:
    title = str(song.get("title") or "未知歌曲")
    count = int(song.get("count") or 0)
    if mode == "按歌手":
        return f"{title} ({count})"
    singer = str(song.get("original_singer") or "未知")
    return f"{title} · {singer} ({count})"


def _build_compact_song_pages(
    songs: list[dict[str, Any]],
    mode: str,
    singer_appearances: Counter[str] | None = None,
) -> list[list[list[tuple[str, Text2Image]]]]:
    columns_per_page = 4
    column_width = 360
    column_height = 1160
    item_gap = 8
    heading_gap = 10
    pages: list[list[list[tuple[str, Text2Image]]]] = [[[]]]
    heights = [[0]]

    def current_column() -> list[tuple[str, Text2Image]]:
        return pages[-1][-1]

    def current_height() -> int:
        return heights[-1][-1]

    def next_column() -> None:
        if len(pages[-1]) >= columns_per_page:
            pages.append([[]])
            heights.append([0])
        else:
            pages[-1].append([])
            heights[-1].append(0)

    def append(kind: str, image: Text2Image, gap: int) -> None:
        current_column().append((kind, image))
        heights[-1][-1] += image.height + gap

    for group_name, group_songs in _prepare_song_list_groups(
        songs,
        mode,
        singer_appearances,
    ):
        heading_text = (
            _wrapped(group_name, 19, column_width, weight="bold", fill=ACCENT)
            if group_name is not None
            else None
        )
        item_images = [
            _wrapped(_compact_song_line(song, mode), 17, column_width)
            for song in group_songs
        ]
        if not item_images:
            continue

        for index, item_image in enumerate(item_images):
            needs_heading = heading_text is not None and (
                index == 0 or not current_column()
            )
            required = item_image.height + item_gap
            if needs_heading:
                required += heading_text.height + heading_gap
            if current_column() and current_height() + required > column_height:
                next_column()
                needs_heading = heading_text is not None
                required = item_image.height + item_gap
                if needs_heading:
                    required += heading_text.height + heading_gap
            if needs_heading and heading_text is not None:
                append(
                    "heading",
                    _wrapped(
                        group_name,
                        19,
                        column_width,
                        weight="bold",
                        fill=ACCENT,
                    ),
                    heading_gap,
                )
            append("item", item_image, item_gap)

    result = [page for page in pages if any(page)]
    if mode in {"按顺序", "按次数", "按歌名"}:
        for page in result:
            if len(page) != 1 or len(page[0]) < 12:
                continue
            entries = page[0]
            total_height = sum(
                item.height + (10 if kind == "heading" else 8)
                for kind, item in entries
            )
            if total_height < 520:
                continue
            split_at = 1
            consumed = 0
            for index, (kind, item) in enumerate(entries[:-1], start=1):
                consumed += item.height + (10 if kind == "heading" else 8)
                split_at = index
                if consumed >= total_height / 2:
                    break
            page[:] = [entries[:split_at], entries[split_at:]]
    return result


def _draw_compact_song_list_page(
    target_date: date | None,
    mode: str,
    song_count: int,
    page_columns: list[list[tuple[str, Text2Image]]],
    page_number: int,
    page_count: int,
) -> BuildImage:
    padding = 46
    column_width = 360
    column_gap = 30
    columns_per_page = len(page_columns)
    width = max(
        720,
        padding * 2
        + columns_per_page * column_width
        + (columns_per_page - 1) * column_gap,
    )

    label = _text("歌单", 18, weight="bold", fill=ACCENT)
    title_value = target_date.strftime("%Y-%m-%d") if target_date else "全部歌曲"
    title = _text(title_value, 36, weight="bold")
    meta = _text(
        f"共 {song_count} 首 · {mode} · {page_number}/{page_count}",
        20,
        fill=MUTED,
    )
    y = padding
    list_y = (
        y
        + label.height
        + 8
        + title.height
        + 9
        + meta.height
        + 28
    )
    content_height = max(
        (
            sum(item.height + (10 if kind == "heading" else 8) for kind, item in column)
            for column in page_columns
        ),
        default=0,
    )
    footer_height = 0
    height = max(
        420,
        list_y + content_height + 28 + footer_height + padding,
    )
    canvas = BuildImage.new("RGBA", (width, height), BACKGROUND)

    label.draw_on_image(canvas.image, (padding, y))
    y += label.height + 8
    title.draw_on_image(canvas.image, (padding, y))
    y += title.height + 9
    meta.draw_on_image(canvas.image, (padding, y))

    for column_index, column in enumerate(page_columns):
        x = padding + column_index * (column_width + column_gap)
        item_y = list_y
        for kind, item in column:
            item.draw_on_image(canvas.image, (x, item_y))
            item_y += item.height + (10 if kind == "heading" else 8)

    return canvas


async def render_song_list(
    target_date: date | None = None,
    mode: str | None = None,
) -> dict[str, Any] | None:
    if mode is None:
        mode = "按顺序" if target_date is not None else "按次数"
    if mode not in SONG_LIST_MODES:
        raise ValueError(f"unknown song list mode: {mode}")
    songs = list_song_catalog(target_date)
    if not songs:
        return None
    await prefetch_emoji_assets(
        [
            *(str(song.get("title") or "") for song in songs),
            *(str(song.get("original_singer") or "") for song in songs),
        ]
    )
    singer_appearances = None
    if mode == "按歌手":
        reference_songs = songs if target_date is None else list_song_catalog()
        singer_appearances = _count_singer_appearances(reference_songs)
    page_columns = _build_compact_song_pages(songs, mode, singer_appearances)
    save_dir = ROOT / "data" / "images" / "song_list"
    save_dir.mkdir(parents=True, exist_ok=True)
    image_paths: list[str] = []
    page_count = len(page_columns)
    for page_number, columns in enumerate(page_columns, start=1):
        canvas = _draw_compact_song_list_page(
            target_date,
            mode,
            len(songs),
            columns,
            page_number,
            page_count,
        )
        date_label = target_date.strftime("%Y%m%d") if target_date else "all"
        save_path = save_dir / (
            f"song_list_{date_label}_{mode}_{page_number}_{uuid.uuid4().hex[:6]}.png"
        )
        canvas.image.convert("RGB").save(save_path, format="PNG", optimize=True)
        image_paths.append(str(save_path))
    return {
        "image_paths": image_paths,
        "data": {
            "date": target_date.strftime("%Y-%m-%d") if target_date else None,
            "count": len(songs),
            "mode": mode,
        },
    }
