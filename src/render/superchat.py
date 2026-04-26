import datetime
import math
import unicodedata
from PIL import Image, ImageDraw

from pathlib import Path
from nonebot_plugin_imageutils import Text2Image

from src.db.event import list_superchat_event_by_day
from src.spider.wrapper import get_name_by_roomid

ROOT = Path(__file__).resolve().parents[2]

def _row_bg_color(amount: int) -> tuple[int, int, int]:
    """根据醒目留言的金额返回对应的背景色"""
    if amount >= 2000:
        return (0xAB, 0x1A, 0x32)  # #AB1A32
    elif amount >= 1000:
        return (0xE5, 0x4D, 0x4D)  # #E54D4D
    elif amount >= 500:
        return (0xE0, 0x94, 0x43)  # #E09443
    elif amount >= 100:
        return (0xE2, 0xB5, 0x2B)  # #E2B52B
    elif amount >= 50:
        return (0x42, 0x7D, 0x9E)  # #427D9E
    elif amount >= 30:
        return (0x2A, 0x60, 0xB2)  # #2A60B2
    elif amount >= 2:
        return (0xDD, 0xDD, 0xDD)  # #DDDDDD
    else:
        return (0xFF, 0xFF, 0xFF)  # #FFFFFF

def truncate_name(name: str, max_len: int = 18) -> str:
    """按东亚字符宽度截断用户名，超出部分加省略号"""
    current_width = 0
    truncated_str = ""
    
    # 预留出省略号 "..." 的宽度
    limit = max_len - 3
    
    # 首先检查总宽度，如果没超限直接返回
    total_width = sum(2 if unicodedata.east_asian_width(c) in 'WFA' else 1 for c in name)
    if total_width <= max_len:
        return name

    for char in name:
        # 判断当前字符宽度
        width = 2 if unicodedata.east_asian_width(char) in 'WFA' else 1
        
        if current_width + width <= limit:
            truncated_str += char
            current_width += width
        else:
            break
            
    return truncated_str + "..."

def draw_text(base_image: Image.Image, x: int, y: int, text: str, fill: tuple, font_size: int, max_width: int = 0) -> int:
    """渲染文本并粘贴到原图上，返回文本图片的高度"""
    if not text:
        return 0
    
    t2i = Text2Image.from_text(text, font_size, fill=fill)
    
    if max_width > 0:
        t2i.wrap(max_width) # 如果设置了最大宽度，自动折行
    
    text_img = t2i.to_image()
    # paste 需要第三个参数作为 mask 保证透明背景正常
    base_image.paste(text_img, (int(x), int(y)), text_img)
    return text_img.height

def generate_superchat_image(data_list: list, room_name: str, date_str: str, part_idx: int) -> Image.Image | None:
    if len(data_list) == 0:
        return None

    font_size = 14
    padding_x = 10
    padding_y = 8
    header_height = 40
    
    col_widths = {
        'time': 80,
        'uname': 140,
        'price': 60,
        'content': 350
    }
    img_width = sum(col_widths.values()) + padding_x * 2
    content_max_width = col_widths['content'] - 5

    # 预处理行高计算
    processed_rows = []
    total_height = header_height
    for item in data_list:
        content_str = str(item['content'])

        bg_color = _row_bg_color(item['price'])
        text_color = (255, 255, 255) if item['price'] >= 30 else (0, 0, 0)

        content_t2i = Text2Image.from_text(content_str, font_size, fill=text_color)
        content_t2i.wrap(content_max_width)
        content_img = content_t2i.to_image(bg_color=bg_color)

        text_height = content_img.height
        row_height = max(30, text_height + padding_y * 2)
        
        processed_rows.append({
            'item': item,
            'content_img': content_img,
            'row_height': row_height,
            'bg_color': bg_color,
            'text_color': text_color
        })
        total_height += row_height

    image = Image.new("RGBA", (img_width, int(total_height)), (255, 255, 255, 255))
    draw = ImageDraw.Draw(image)

    # 画标题，定义标题背景色
    title_bg = (240, 240, 240)
    title_prefix = f"{room_name}的醒目留言" if room_name else "醒目留言"
    title_text = f"{title_prefix}（{date_str}）- 第{part_idx}页"
    draw.rectangle([0, 0, img_width, header_height], fill=title_bg)
    
    # 居中标题：使用实心背景解决灰边，并精确计算居中高度
    title_t2i = Text2Image.from_text(title_text, font_size, fill=(0,0,0))
    title_img = title_t2i.to_image(bg_color=title_bg)
    title_x = (img_width - title_img.width) / 2
    title_y = (header_height - title_img.height) / 2
    image.paste(title_img, (int(title_x), int(title_y)))

    current_y = header_height
    for row_data in processed_rows:
        item = row_data['item']
        row_height = row_data['row_height']
        content_img = row_data['content_img']
        text_color = row_data['text_color']
        bg_color = row_data['bg_color']

        draw.rectangle([0, current_y, img_width, current_y + row_height], fill=bg_color)
        
        time_str = datetime.datetime.fromtimestamp(item['timestamp']).strftime("%H:%M:%S")
        uname = truncate_name(str(item['uname']))
        price = f"￥{item['price']}"

        current_x = padding_x
        
        # 依次渲染各列信息：使用 Text2Image 并传入本行专属的 bg_color
        
        # 1. 时间
        time_img = Text2Image.from_text(time_str, font_size, fill=text_color).to_image(bg_color=bg_color)
        image.paste(time_img, (int(current_x), int(current_y + padding_y)))
        current_x += col_widths['time']
        
        # 2. 名字
        uname_img = Text2Image.from_text(uname, font_size, fill=text_color).to_image(bg_color=bg_color)
        image.paste(uname_img, (int(current_x), int(current_y + padding_y)))
        current_x += col_widths['uname']
        
        # 3. 金额
        price_img = Text2Image.from_text(price, font_size, fill=text_color).to_image(bg_color=bg_color)
        image.paste(price_img, (int(current_x), int(current_y + padding_y)))
        current_x += col_widths['price']

        # 4. 内容
        image.paste(content_img, (int(current_x), int(current_y + padding_y))) 
        
        current_y += row_height

    return image.convert("RGB")


async def get_daily_superchat_images(room_id: int, day: datetime.datetime, chunk_size: int = 40) -> list[Path]:
    """
    分片缓存逻辑：处理按设定大小切分图片，返回列表
    """
    date_str = day.strftime('%Y-%m-%d')
    # 按房间号和日期独立建立目录
    image_dir = ROOT / "data" / "images" / "superchat" / str(room_id) / date_str
    image_dir.mkdir(parents=True, exist_ok=True)
    
    is_today = day.date() == datetime.datetime.now().date()
    data_list = list_superchat_event_by_day(room_id, day)
    
    if not data_list:
        return []

    room_name = await get_name_by_roomid(room_id)
    total_chunks = max(1, math.ceil(len(data_list) / chunk_size))
    generated_paths = []

    for i in range(total_chunks):
        part_idx = i + 1
        chunk_data = data_list[i * chunk_size : (i + 1) * chunk_size]
        is_full = (len(chunk_data) == chunk_size)
        
        cache_path = image_dir / f"part_{part_idx}.png"        
        if cache_path.exists():
            generated_paths.append(cache_path)
            continue

        today = image_dir / "today.png"
        img = generate_superchat_image(chunk_data, room_name, date_str, part_idx)
        if img is None:
            continue
            
        # 只要满了40条或者已经不是今天，就可以用纯 part_{n}.png 永久缓存
        if is_full or not is_today:
            filename = cache_path
        else:
            filename = today

        img.save(filename)
        generated_paths.append(filename)
        
        # 如果缓存了永久切片，清理掉因为遗留导致的 today 缓存
        if is_full:
            if today.exists():
                today.unlink()

    return generated_paths