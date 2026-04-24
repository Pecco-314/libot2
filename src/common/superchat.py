import datetime
import unicodedata
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
from pilmoji import Pilmoji

from src.db.event import list_superchat_event_by_day
from src.db.liver import get_name_by_roomid

# 路径配置
ROOT = Path(__file__).resolve().parents[2]
FONT_PATH = ROOT / "fonts" / "NotoSansCJKsc-Regular.otf"

def _row_bg_color(amount: int) -> tuple[int, int, int]:
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

def wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    lines = []
    for paragraph in text.split('\n'):
        current_line = ""
        for char in paragraph:
            if font.getlength(current_line + char) <= max_width:
                current_line += char
            else:
                if current_line:
                    lines.append(current_line)
                current_line = char
        if current_line:
            lines.append(current_line)
    return lines if lines else [""]

def truncate_name(name: str, max_len: int = 18) -> str:
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

def draw_safe_pilmoji_text(draw, pilmoji, x, y, text, fill, font, font_size, bg_color):
    """
    带占位符的渲染函数：解决纯 Emoji 导致的高度塌陷问题
    """
    dummy_char = "I"
    dummy_width = font.getlength(dummy_char)
    
    # 向左偏移占位符的宽度
    start_x = x - dummy_width
    
    # 带着占位符交给 Pilmoji 渲染，此时高度计算一定会正常
    pilmoji.text((start_x, y), dummy_char + text, fill=fill, font=font)
    
    # 渲染完成后，用当前行的背景色画一个矩形，把占位符抹除掉
    # 右边缘设定为 x - 1，确保不会覆盖到真正的文字或 emoji
    draw.rectangle([
        start_x - 1, 
        y - 2, 
        x - 1, 
        y + font_size + 4
    ], fill=bg_color)

def generate_superchat_image(data_list: list, room_name: str, font_path: Path = FONT_PATH) -> Image.Image | None:
    if len(data_list) == 0:
        return None

    font_size = 14
    padding_x = 10
    padding_y = 8
    line_spacing = 4
    header_height = 40
    
    col_widths = {
        'time': 80,
        'uname': 140,
        'price': 60,
        'content': 350
    }
    img_width = sum(col_widths.values()) + padding_x * 2

    try:
        font = ImageFont.truetype(str(font_path), font_size)
    except OSError:
        font = ImageFont.load_default()

    processed_rows = []
    content_max_width = col_widths['content'] - 5
    
    total_height = header_height
    for item in data_list:
        content_str = str(item['content'])
        wrapped_lines = wrap_text(content_str, font, content_max_width)
        
        text_height = len(wrapped_lines) * font_size + (len(wrapped_lines) - 1) * line_spacing
        row_height = max(30, text_height + padding_y * 2)
        
        processed_rows.append({
            'item': item,
            'lines': wrapped_lines,
            'row_height': row_height
        })
        total_height += row_height

    image = Image.new("RGB", (img_width, int(total_height)), (255, 255, 255))
    draw = ImageDraw.Draw(image)

    with Pilmoji(image) as pilmoji:
        date_str = data_list[0]['created_at'].strftime("%Y-%m-%d")
        title_text = f"{room_name}的醒目留言（{date_str}）" if room_name else f"醒目留言（{date_str}）"
        
        title_width = font.getlength(title_text)
        title_x = (img_width - title_width) / 2
        
        draw.rectangle([0, 0, img_width, header_height], fill=(240, 240, 240))
        pilmoji.text((title_x, (header_height - font_size) // 2), title_text, fill=(0, 0, 0), font=font)

        current_y = header_height
        for row_data in processed_rows:
            item = row_data['item']
            lines = row_data['lines']
            row_height = row_data['row_height']
            
            bg_color = _row_bg_color(item['price'])
            text_color = (255, 255, 255) if item['price'] >= 30 else (0, 0, 0)
            
            draw.rectangle([0, current_y, img_width, current_y + row_height], fill=bg_color)
            
            time_str = item['created_at'].strftime("%H:%M:%S")
            uname = truncate_name(str(item['uname']))
            price = f"￥{item['price']}"

            current_x = padding_x
            
            # 使用安全函数绘制各列文字
            draw_safe_pilmoji_text(draw, pilmoji, current_x, current_y + padding_y, time_str, text_color, font, font_size, bg_color)
            current_x += col_widths['time']
            
            draw_safe_pilmoji_text(draw, pilmoji, current_x, current_y + padding_y, uname, text_color, font, font_size, bg_color)
            current_x += col_widths['uname']
            
            draw_safe_pilmoji_text(draw, pilmoji, current_x, current_y + padding_y, price, text_color, font, font_size, bg_color)
            current_x += col_widths['price']
            
            for i, line in enumerate(lines):
                line_y = current_y + padding_y + i * (font_size + line_spacing)
                # 处理多行文字中可能存在的纯 Emoji 行
                draw_safe_pilmoji_text(draw, pilmoji, current_x, line_y, line, text_color, font, font_size, bg_color)

            current_y += row_height

    return image


def get_daily_superchat_image(room_id: int, day: datetime.datetime) -> Path | None:
    """
    主接口：处理缓存逻辑并调用生成函数
    """
    image_dir = ROOT / "data" / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    
    # 判断是否为今天
    is_today = day.date() == datetime.datetime.now().date()
    if is_today:
        cache_path = image_dir / "superchat" / f"{room_id}_today.png"
    else:
        cache_path = image_dir / "superchat" / f"{room_id}_{day.strftime('%Y-%m-%d')}.png"
    
    # 如果不是今天且缓存存在，直接返回缓存图片
    if not is_today and cache_path.exists():
        return cache_path
        
    # 查询数据
    data_list = list_superchat_event_by_day(room_id, day)

    # 生成图片
    room_name = get_name_by_roomid(room_id)
    img = generate_superchat_image(data_list, room_name)
    
    if img is None:
        return None

    # 保存到缓存
    img.save(cache_path)

    return cache_path