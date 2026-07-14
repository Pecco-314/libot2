import uuid
import re
from datetime import datetime, date
from typing import Any

from nonebot_plugin_imageutils import BuildImage, Text2Image

from src.db.song_list import search_songs_by_title, random_song, list_songs_by_singer, get_songs_of_date
from src.common.utils import ROOT, truncate_name

def _smart_wrap(text: str, font_size: int, max_width: int, weight: str = "normal") -> str:
    """基于英文单词和汉字的智能换行"""
    # 拆分：连续字母数字 / 连续空白 / 单个全角字符(汉字等) / 单个其他字符(标点等)
    tokens = re.findall(r'[a-zA-Z0-9]+|\s+|[^\x00-\xff]|.', text)
    
    lines = []
    curr_line = ""
    
    for token in tokens:
        test_line = curr_line + token
        # 使用 Text2Image 获取真实渲染的像素宽度
        if Text2Image.from_text(test_line, font_size, weight=weight).width > max_width:
            if curr_line:
                lines.append(curr_line.rstrip())
                # 如果当前引发超宽的 token 是个空格，直接丢弃，不放到下一行行首
                curr_line = "" if token.isspace() else token
            else:
                # 极端情况：单个超长单词本身就超过了最大宽度
                lines.append(token)
                curr_line = ""
        else:
            curr_line = test_line
            
    if curr_line:
        lines.append(curr_line.rstrip())
        
    return "\n".join(lines)

def _get_relative_time(date_str: str) -> str:
    """计算演唱时间的相对描述"""
    now_date = datetime.now().date()
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return date_str

    delta = (now_date - dt).days
    if delta == 0:
        return "今天"
    elif delta == 1:
        return "昨天"
    elif delta < 7:
        return f"{delta}天前"
    elif delta < 30:
        return f"{delta // 7}周前"
    elif delta < 365:
        return f"{delta // 30}个月前"
    else:
        return f"{delta // 365}年前"


def draw_song_card(song: dict) -> BuildImage:
    width = 600
    padding = 40
    max_text_width = width - padding * 2
    bg_color = (255, 255, 255, 255)

    # 1. 处理日期
    raw_records = song.get("records", [])
    parsed_dates = []
    for r in raw_records:
        try:
            parsed_dates.append(datetime.strptime(r, "%Y-%m-%d").date())
        except ValueError:
            continue
    parsed_dates.sort(reverse=True)
    recent_dates = parsed_dates[:5]

    # 2. 准备文本对象
    title_text = song["title"]
    wrapped_title = _smart_wrap(title_text, 44, max_text_width, weight="bold")
    title_t2i = Text2Image.from_text(wrapped_title, 44, weight="bold", fill=(34, 34, 34))
    
    raw_singer = song.get('original_singer') or '未知'
    safe_singer = truncate_name(raw_singer, max_len=32)
    singer_t2i = Text2Image.from_text(f"原唱: {safe_singer}", 26, fill=(102, 102, 102))
    
    count_t2i = Text2Image.from_text(f"已演唱 {song.get('count', 0)} 次", 30, fill=(0, 102, 204))
    recent_label_t2i = Text2Image.from_text("最近演唱：", 28, fill=(50, 50, 50))

    date_items = []
    for d in recent_dates:
        d_str = d.strftime("%Y-%m-%d")
        rel = _get_relative_time(d_str)
        item = Text2Image.from_text(f"· {d_str} ({rel})", 26, fill=(80, 80, 80))
        date_items.append(item)
    if not date_items:
        date_items.append(Text2Image.from_text("暂无日期记录", 26, fill=(150, 150, 150)))

    footer_text = "数据来源于三理Mit3uri的歌单（mit3uri.live），感谢作者。"
    footer_t2i = Text2Image.from_text(footer_text, 18, fill=(180, 180, 180))

    # 3. 动态计算画布高度
    content_h = (
        padding + 
        title_t2i.height + 15 +
        singer_t2i.height + 35 +
        count_t2i.height + 40 +
        recent_label_t2i.height + 15 +
        (len(date_items) * 38) + 40 +
        footer_t2i.height + 
        padding
    )

    canvas = BuildImage.new("RGBA", (width, int(content_h)), bg_color)
    
    # 4. 顺序绘制
    curr_y = padding
    title_t2i.draw_on_image(canvas.image, (padding, curr_y))
    curr_y += title_t2i.height + 15
    singer_t2i.draw_on_image(canvas.image, (padding, curr_y))
    curr_y += singer_t2i.height + 35
    count_t2i.draw_on_image(canvas.image, (padding, curr_y))
    curr_y += count_t2i.height + 40
    recent_label_t2i.draw_on_image(canvas.image, (padding, curr_y))
    curr_y += recent_label_t2i.height + 15
    for d_img in date_items:
        d_img.draw_on_image(canvas.image, (padding + 20, curr_y))
        curr_y += 38
    curr_y += 40
    footer_t2i.draw_on_image(canvas.image, (width - footer_t2i.width - padding, curr_y))

    return canvas


def save_song_card(song: dict) -> dict[str, Any]:
    """调用绘图函数并保存到本地，返回路径和数据"""
    save_dir = ROOT / "data" / "images" / "song_list"
    save_dir.mkdir(parents=True, exist_ok=True)

    canvas = draw_song_card(song)
    
    file_name = f"song_{song['id']}_{uuid.uuid4().hex[:6]}.png"
    save_path = save_dir / file_name
    canvas.image.save(save_path)

    return {
        "image_path": str(save_path),
        "data": song
    }


async def render_songs_by_keyword(keyword: str, limit: int = 5) -> list[dict[str, Any]]:
    """搜索歌曲并渲染"""
    songs = search_songs_by_title(keyword, limit)
    return [save_song_card(song) for song in songs]


async def render_random_song(limit: int = 3) -> dict[str, Any] | None:
    """随机抽取歌曲并渲染"""
    song = random_song(limit) 
    if song is None:
        return None
    else:
        return save_song_card(song)


def _draw_singer_song_list(singer: str, songs: list[dict[str, Any]]) -> BuildImage:
    width = 720
    padding = 40
    max_text_width = width - padding * 2
    bg_color = (255, 255, 255, 255)

    safe_singer = truncate_name(singer, max_len=32)
    title_t2i = Text2Image.from_text(f"歌手：{safe_singer}", 36, weight="bold", fill=(34, 34, 34))
    count_t2i = Text2Image.from_text(f"共 {len(songs)} 首", 24, fill=(80, 80, 80))

    line_items: list[Text2Image] = []
    for index, song in enumerate(songs, start=1):
        line = f"{index}. {song['title']}（{song.get('count', 0)}）"
        wrapped = _smart_wrap(line, 24, max_text_width)
        line_items.append(Text2Image.from_text(wrapped, 24, fill=(50, 50, 50)))

    footer_text = "数据来源于三理Mit3uri的歌单（mit3uri.live），感谢作者。"
    footer_t2i = Text2Image.from_text(footer_text, 18, fill=(180, 180, 180))

    content_h = (
        padding +
        title_t2i.height + 12 +
        count_t2i.height + 24 +
        sum(item.height + 10 for item in line_items) +
        20 +
        footer_t2i.height +
        padding
    )

    canvas = BuildImage.new("RGBA", (width, int(content_h)), bg_color)
    curr_y = padding
    title_t2i.draw_on_image(canvas.image, (padding, curr_y))
    curr_y += title_t2i.height + 12
    count_t2i.draw_on_image(canvas.image, (padding, curr_y))
    curr_y += count_t2i.height + 24
    for item in line_items:
        item.draw_on_image(canvas.image, (padding, curr_y))
        curr_y += item.height + 10
    curr_y += 20
    footer_t2i.draw_on_image(canvas.image, (width - footer_t2i.width - padding, curr_y))

    return canvas


def _save_singer_song_list(singer: str, songs: list[dict[str, Any]]) -> str:
    save_dir = ROOT / "data" / "images" / "song_list"
    save_dir.mkdir(parents=True, exist_ok=True)
    canvas = _draw_singer_song_list(singer, songs)
    file_name = f"singer_{uuid.uuid4().hex[:8]}.png"
    save_path = save_dir / file_name
    canvas.image.save(save_path)
    return str(save_path)


async def render_songs_by_singer(singer: str) -> dict[str, Any] | None:
    songs = list_songs_by_singer(singer)
    if not songs:
        return None
    image_path = _save_singer_song_list(singer, songs)
    return {
        "image_path": image_path,
        "data": {
            "singer": singer,
            "count": len(songs),
        },
    }


def _draw_song_list_by_date(target_date: date, songs: list[dict[str, Any]]) -> BuildImage:
    width = 760
    padding = 40
    max_text_width = width - padding * 2
    bg_color = (255, 255, 255, 255)

    date_text = target_date.strftime("%Y-%m-%d")
    title_t2i = Text2Image.from_text(f"歌单：{date_text}", 36, weight="bold", fill=(34, 34, 34))
    count_t2i = Text2Image.from_text(f"共 {len(songs)} 首", 24, fill=(80, 80, 80))

    line_items: list[Text2Image] = []
    for index, song in enumerate(songs, start=1):
        singer = song.get("original_singer") or "未知"
        line = f"{index}. {song['title']} - {truncate_name(singer, max_len=24)}（{song.get('count', 0)}）"
        wrapped = _smart_wrap(line, 24, max_text_width)
        line_items.append(Text2Image.from_text(wrapped, 24, fill=(50, 50, 50)))

    footer_text = "数据来源于三理Mit3uri的歌单（mit3uri.live），感谢作者。"
    footer_t2i = Text2Image.from_text(footer_text, 18, fill=(180, 180, 180))

    content_h = (
        padding
        + title_t2i.height
        + 12
        + count_t2i.height
        + 24
        + sum(item.height + 10 for item in line_items)
        + 20
        + footer_t2i.height
        + padding
    )

    canvas = BuildImage.new("RGBA", (width, int(content_h)), bg_color)
    curr_y = padding
    title_t2i.draw_on_image(canvas.image, (padding, curr_y))
    curr_y += title_t2i.height + 12
    count_t2i.draw_on_image(canvas.image, (padding, curr_y))
    curr_y += count_t2i.height + 24
    for item in line_items:
        item.draw_on_image(canvas.image, (padding, curr_y))
        curr_y += item.height + 10
    curr_y += 20
    footer_t2i.draw_on_image(canvas.image, (width - footer_t2i.width - padding, curr_y))

    return canvas


def _save_song_list_by_date(target_date: date, songs: list[dict[str, Any]]) -> str:
    save_dir = ROOT / "data" / "images" / "song_list"
    save_dir.mkdir(parents=True, exist_ok=True)
    canvas = _draw_song_list_by_date(target_date, songs)
    file_name = f"song_list_{target_date.strftime('%Y%m%d')}_{uuid.uuid4().hex[:6]}.png"
    save_path = save_dir / file_name
    canvas.image.save(save_path)
    return str(save_path)


async def render_songs_by_date(target_date: date) -> dict[str, Any] | None:
    songs = get_songs_of_date(target_date)
    if not songs:
        return None
    image_path = _save_song_list_by_date(target_date, songs)
    return {
        "image_path": image_path,
        "data": {
            "date": target_date.strftime("%Y-%m-%d"),
            "count": len(songs),
        },
    }