import json
import re
import sys
import argparse
import requests
import math
from pathlib import Path
from io import BytesIO
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from PIL import Image, ImageDraw, ImageFont
from pilmoji import Pilmoji

ROOT = Path(__file__).resolve().parents[2]
FONT_PATH = ROOT / "fonts" / "NotoSansCJKsc-Regular.otf"
IMG_CACHE = {} 

# 定义 B 站缩略图常用的后缀参数
SUFFIX_AVATAR = "120w_120h_1e_1c.webp"
SUFFIX_GRID = "400w_400h_1e_1c.webp"
SUFFIX_SINGLE = "1080w.webp"

def get_bili_optimized_url(url, suffix):
    """为 B 站图片添加缩略图后缀"""
    if not url or "hdslb.com" not in url:
        return url
    base_url = url.split('@')[0]
    return f"{base_url}@{suffix}"

def download_image(url, suffix=None):
    """下载图片并转换为 RGBA 格式，使用全局缓存防重"""
    if not url:
        return Image.new("RGBA", (100, 100), (200, 200, 200, 255))
        
    if not url.startswith('http'):
        url = 'https:' + url if url.startswith('//') else url
    
    # 优先使用优化后的 URL
    target_url = get_bili_optimized_url(url, suffix) if suffix else url
    
    if target_url in IMG_CACHE:
        return IMG_CACHE[target_url].copy()
        
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://t.bilibili.com/"
        }
        response = requests.get(target_url, headers=headers, timeout=10)
        response.raise_for_status()
        img = Image.open(BytesIO(response.content)).convert("RGBA")
        IMG_CACHE[target_url] = img
        return img.copy()
    except Exception as e:
        print(f"[警告] 无法下载图片 {target_url}: {e}")
        img = Image.new("RGBA", (100, 100), (200, 200, 200, 255))
        IMG_CACHE[target_url] = img
        return img

def pre_download_assets(main_info, origin_info, emoji_dict):
    """在渲染开始前，并发预下载所有需要的图片到缓存"""
    tasks = []
    
    # 主动态头像
    if main_info.get('avatar_url'):
        tasks.append((main_info['avatar_url'], SUFFIX_AVATAR))
    
    # 主动态配图
    pics = main_info.get('pic_urls', [])
    p_suffix = SUFFIX_SINGLE if len(pics) == 1 else SUFFIX_GRID
    for u in pics:
        tasks.append((u, p_suffix))
        
    # 转发动态内容
    if origin_info:
        if origin_info.get('avatar_url'):
            tasks.append((origin_info['avatar_url'], SUFFIX_AVATAR))
        o_pics = origin_info.get('pic_urls', [])
        op_suffix = SUFFIX_SINGLE if len(o_pics) == 1 else SUFFIX_GRID
        for u in o_pics:
            tasks.append((u, op_suffix))
            
    # 自定义表情
    for e_url in emoji_dict.values():
        tasks.append((e_url, None))

    # 使用线程池并发执行下载任务
    with ThreadPoolExecutor(max_workers=10) as executor:
        executor.map(lambda p: download_image(p[0], p[1]), tasks)

def create_circular_avatar(img, size):
    """将图片裁剪为高清晰度的圆形头像"""
    img = img.resize((size * 3, size * 3), Image.Resampling.LANCZOS)
    mask = Image.new('L', (size * 3, size * 3), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, size * 3, size * 3), fill=255)
    img.putalpha(mask)
    return img.resize((size, size), Image.Resampling.LANCZOS)

def crop_center_square(img):
    width, height = img.size
    size = min(width, height)
    left = (width - size) // 2
    top = (height - size) // 2
    right = left + size
    bottom = top + size
    return img.crop((left, top, right, bottom))

def extract_dynamic_info(data_dict, dy_type):
    username = "未知用户"
    avatar_url = ""
    text = ""
    pic_urls = []

    if dy_type == 8: 
        owner = data_dict.get('owner') or {}
        username = owner.get('name', '未知用户')
        avatar_url = owner.get('face', '')
        title = data_dict.get('title', '')
        text = f"▶ 视频投稿：{title}"
        if data_dict.get('pic'):
            pic_urls = [data_dict.get('pic')]
            
    elif dy_type == 1: 
        user = data_dict.get('user') or {}
        username = user.get('uname', '未知用户')
        avatar_url = user.get('face', '')
        item = data_dict.get('item') or {}
        text = item.get('content', '')
        
    elif dy_type == 2: 
        user = data_dict.get('user') or {}
        username = user.get('name', '未知用户')
        avatar_url = user.get('head_url', '')
        item = data_dict.get('item') or {}
        text = item.get('description', '')
        pics = item.get('pictures') or []
        pic_urls = [p.get('img_src') for p in pics if 'img_src' in p]
        
    else: 
        user = data_dict.get('user') or data_dict.get('owner') or {}
        username = user.get('name') or user.get('uname') or "未知用户"
        avatar_url = user.get('head_url') or user.get('face') or ""
        item = data_dict.get('item') or {}
        text = item.get('description') or item.get('content') or data_dict.get('desc') or data_dict.get('dynamic') or ""
        pics = item.get('pictures') or []
        pic_urls = [p.get('img_src') for p in pics if 'img_src' in p]
        if not pic_urls and data_dict.get('pic'):
            pic_urls = [data_dict.get('pic')]
            
    return {
        'username': username,
        'avatar_url': avatar_url,
        'text': text.strip(),
        'pic_urls': pic_urls
    }

def wrap_and_tokenize_text(text, font, max_width, emoji_dict, emoji_size):
    if not text:
        return []
    lines = []
    emoji_padding = 6 
    total_emoji_w = emoji_size + emoji_padding * 2
    pattern = None
    if emoji_dict:
        pattern = re.compile('(' + '|'.join(map(re.escape, emoji_dict.keys())) + ')')
    
    for paragraph in text.split('\n'):
        tokens = [t for t in pattern.split(paragraph) if t] if pattern else [paragraph]
        current_line = []
        current_x = 0
        for token in tokens:
            if emoji_dict and token in emoji_dict:
                if current_x + total_emoji_w > max_width and current_line:
                    lines.append(current_line)
                    current_line = []
                    current_x = 0
                current_line.append({'type': 'emoji', 'url': emoji_dict[token], 'x': current_x + emoji_padding})
                current_x += total_emoji_w
            else:
                for char in token:
                    char_w = font.getlength(char) if hasattr(font, 'getlength') else font.size
                    if current_x + char_w > max_width and current_line:
                        lines.append(current_line)
                        current_line = []
                        current_x = 0
                    if current_line and current_line[-1]['type'] == 'text':
                        current_line[-1]['content'] += char
                    else:
                        current_line.append({'type': 'text', 'content': char, 'x': current_x})
                    current_x += char_w
        lines.append(current_line)
    return lines

def render_text_and_images(text, pic_urls, width, font, emoji_dict, text_color):
    if not text and not pic_urls:
        return None
    emoji_size = int(font.size * 1.5)
    line_height = int(font.size * 1.6)
    lines = wrap_and_tokenize_text(text, font, width, emoji_dict, emoji_size)
    text_height = len(lines) * line_height if lines else 0
    img_spacing = 8
    imgs_height = 0
    img_layout = []
    pic_count = len(pic_urls)
    
    if pic_count > 0:
        p_suffix = SUFFIX_SINGLE if pic_count == 1 else SUFFIX_GRID
        if pic_count == 1:
            img = download_image(pic_urls[0], p_suffix)
            ratio = width / float(img.width)
            new_height = int(img.height * ratio)
            img = img.resize((width, new_height), Image.Resampling.LANCZOS)
            img_layout.append((img, 0, 0))
            imgs_height = new_height
        else:
            columns = 2 if pic_count in (2, 4) else 3
            square_size = (width - (columns - 1) * img_spacing) // columns
            rows = math.ceil(pic_count / columns)
            imgs_height = rows * square_size + (rows - 1) * img_spacing
            for i, url in enumerate(pic_urls):
                img = download_image(url, p_suffix)
                img = crop_center_square(img)
                img = img.resize((square_size, square_size), Image.Resampling.LANCZOS)
                row, col = i // columns, i % columns
                x = col * (square_size + img_spacing)
                y = row * (square_size + img_spacing)
                img_layout.append((img, x, y))
        
    total_height = text_height + imgs_height + (10 if text_height > 0 and imgs_height > 0 else 0)
    if total_height <= 0: return None
        
    canvas = Image.new("RGBA", (width, total_height), (255, 255, 255, 0))
    draw_y = 0
    if lines:
        with Pilmoji(canvas) as pilmoji:
            for line in lines:
                if not line:
                    draw_y += line_height
                    continue
                for el in line:
                    if el['type'] == 'text':
                        pilmoji.text((el['x'], draw_y), el['content'], font=font, fill=text_color)
                    elif el['type'] == 'emoji':
                        e_img = download_image(el['url']) # 表情包通常已经在缓存且无后缀
                        e_img = e_img.resize((emoji_size, emoji_size), Image.Resampling.LANCZOS)
                        y_offset = max(0, (line_height - emoji_size) // 2) - 2
                        canvas.paste(e_img, (int(el['x']), int(draw_y + y_offset)), mask=e_img)
                draw_y += line_height
                
    if img_layout:
        base_y = draw_y + (10 if text_height > 0 else 0)
        for img, x, y in img_layout:
            canvas.paste(img, (int(x), int(base_y + y)), mask=img)
    return canvas

def render_bilibili_card(card_json_str, dy_type, orig_type, timestamp, emoji_details=None, width=800):
    try:
        data = json.loads(card_json_str)
    except json.JSONDecodeError:
        raise ValueError("无效 JSON")

    emoji_dict = {e['emoji_name']: e['url'] for e in (emoji_details or [])}
    main_info = extract_dynamic_info(data, dy_type)
    
    origin_info = None
    if 'origin' in data and data['origin'] and data['origin'] != 'null':
        try:
            origin_data = json.loads(data['origin'])
            origin_info = extract_dynamic_info(origin_data, orig_type)
            origin_info['text'] = f"@{origin_info['username']}: {origin_info['text']}"
        except json.JSONDecodeError:
            pass

    # [关键优化点] 渲染前先并发下载所有资源
    pre_download_assets(main_info, origin_info, emoji_dict)

    margin, avatar_size = 40, 80
    name_font = ImageFont.truetype(FONT_PATH, 32)
    time_font = ImageFont.truetype(FONT_PATH, 24)
    content_font = ImageFont.truetype(FONT_PATH, 28)

    time_str = datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M') if timestamp else "未知时间"
    main_width = width - margin * 2

    # A：头部
    header_height = max(avatar_size, 32 + 24 + 10)
    header_canvas = Image.new("RGBA", (main_width, header_height), (255, 255, 255, 0))
    header_draw = ImageDraw.Draw(header_canvas)
    if main_info['avatar_url']:
        avatar_img = download_image(main_info['avatar_url'], SUFFIX_AVATAR)
        avatar_img = create_circular_avatar(avatar_img, avatar_size)
        header_canvas.paste(avatar_img, (0, 0), mask=avatar_img)
    header_draw.text((avatar_size + 20, 0), main_info['username'], font=name_font, fill=(251, 114, 153, 255))
    header_draw.text((avatar_size + 20, 42), time_str, font=time_font, fill=(153, 153, 153, 255))

    # B：主体
    main_content_canvas = render_text_and_images(main_info['text'], main_info['pic_urls'], main_width, content_font, emoji_dict, (34, 34, 34, 255))

    # C：转发框
    origin_canvas = None
    if origin_info:
        origin_margin = 24
        origin_inner_width = main_width - origin_margin * 2
        origin_inner_canvas = render_text_and_images(origin_info['text'], origin_info['pic_urls'], origin_inner_width, content_font, emoji_dict, (102, 102, 102, 255))
        if origin_inner_canvas:
            box_height = origin_inner_canvas.height + origin_margin * 2
            origin_canvas = Image.new("RGBA", (main_width, box_height), (255, 255, 255, 0))
            ImageDraw.Draw(origin_canvas).rounded_rectangle((0, 0, main_width, box_height), radius=12, fill=(244, 245, 247, 255))
            origin_canvas.paste(origin_inner_canvas, (origin_margin, origin_margin), origin_inner_canvas)

    # 组装
    total_y = margin + header_height + 30
    if main_content_canvas: total_y += main_content_canvas.height + 20
    if origin_canvas: total_y += origin_canvas.height + 20
    total_y += margin - 20

    final_image = Image.new("RGBA", (width, total_y), (255, 255, 255, 255))
    curr_y = margin
    final_image.paste(header_canvas, (margin, curr_y), header_canvas)
    curr_y += header_height + 30
    if main_content_canvas:
        final_image.paste(main_content_canvas, (margin, curr_y), main_content_canvas)
        curr_y += main_content_canvas.height + 20
    if origin_canvas:
        final_image.paste(origin_canvas, (margin, curr_y), origin_canvas)

    return final_image

if __name__ == "__main__":
    timestamp=1776962919
    dy_type=2
    orig_type=0
    card_json_str = "{\"item\":{\"id\":392340666,\"description\":\"感觉刚咂巴咂巴尝到味！\\n明天来肖申克救赎加打工了\\n有意思！！\\n\\n这段时间流感都还蛮严重的\\n池要好好休息注意防护噢！\\n\\noya🌽\\n传输好梦🍀\\n\\n\",\"pictures\":[{\"img_height\":1080,\"img_width\":1123,\"img_src\":\"http://i0.hdslb.com/bfs/new_dyn/3cb23b717d029a417f9f56b3fac7b3ed2030198123.png\",\"img_size\":1861.888}],\"pictures_count\":1,\"reply\":120,\"upload_time\":1776962919},\"user\":{\"uid\":2030198123,\"name\":\"三理Mit3uri\",\"head_url\":\"https://i2.hdslb.com/bfs/face/3dd1f948bef06a933084e7231fa0708bea6410aa.jpg\",\"vip\":{\"type\":2,\"due_date\":1803916800000,\"status\":1,\"theme_type\":0,\"label\":{\"path\":\"http://i0.hdslb.com/bfs/vip/label_annual.png\",\"text\":\"年度大会员\",\"label_theme\":\"annual_vip\",\"text_color\":\"\",\"bg_style\":0,\"bg_color\":\"\",\"border_color\":\"\"},\"avatar_subscript\":1,\"nickname_color\":\"#FB7299\",\"vip_pay_type\":1}}}"
    img = render_bilibili_card(card_json_str, dy_type, orig_type, timestamp)
    if img:
        img.save("test_output.png")