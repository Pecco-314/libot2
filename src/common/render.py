import json
import re
import sys
import argparse
import requests
import math
from pathlib import Path
from io import BytesIO
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont
from pilmoji import Pilmoji

ROOT = Path(__file__).resolve().parents[2]
FONT_PATH = ROOT / "fonts" / "NotoSansCJKsc-Regular.otf"
IMG_CACHE = {} # 图片缓存，加快生成所有动态时的速度

def download_image(url):
    """下载图片并转换为 RGBA 格式，使用全局缓存防重"""
    if not url:
        return Image.new("RGBA", (100, 100), (200, 200, 200, 255))
        
    if not url.startswith('http'):
        url = 'https:' + url if url.startswith('//') else url
        
    if url in IMG_CACHE:
        return IMG_CACHE[url].copy()
        
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        img = Image.open(BytesIO(response.content)).convert("RGBA")
        IMG_CACHE[url] = img
        return img.copy()
    except Exception as e:
        print(f"[警告] 无法下载图片 {url}: {e}")
        img = Image.new("RGBA", (100, 100), (200, 200, 200, 255))
        IMG_CACHE[url] = img
        return img

def create_circular_avatar(img, size):
    """将图片裁剪为高清晰度的圆形头像"""
    img = img.resize((size * 3, size * 3), Image.Resampling.LANCZOS)
    mask = Image.new('L', (size * 3, size * 3), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, size * 3, size * 3), fill=255)
    img.putalpha(mask)
    return img.resize((size, size), Image.Resampling.LANCZOS)

def crop_center_square(img):
    """将图片裁剪为居中的正方形"""
    width, height = img.size
    size = min(width, height)
    left = (width - size) // 2
    top = (height - size) // 2
    right = left + size
    bottom = top + size
    return img.crop((left, top, right, bottom))

def extract_dynamic_info(data_dict, dy_type):
    """根据 type 从解析后的 JSON 字典中提取标准化的动态信息"""
    username = "未知用户"
    avatar_url = ""
    text = ""
    pic_urls = []

    if dy_type == 8: # 视频投稿
        owner = data_dict.get('owner') or {}
        username = owner.get('name', '未知用户')
        avatar_url = owner.get('face', '')
        title = data_dict.get('title', '')
        # 移除 desc，真实 B 站动态卡片不展示长长的视频简介，仅展示标题
        text = f"▶ 视频投稿：{title}"
        if data_dict.get('pic'):
            pic_urls = [data_dict.get('pic')]
            
    elif dy_type == 1: # 转发
        user = data_dict.get('user') or {}
        username = user.get('uname', '未知用户')
        avatar_url = user.get('face', '')
        item = data_dict.get('item') or {}
        text = item.get('content', '')
        # 原动态内容由外层提取，此处不管 pic_urls
        
    elif dy_type == 2: # 图文/纯文本
        user = data_dict.get('user') or {}
        username = user.get('name', '未知用户')
        avatar_url = user.get('head_url', '')
        item = data_dict.get('item') or {}
        text = item.get('description', '')
        pics = item.get('pictures') or []
        pic_urls = [p.get('img_src') for p in pics if 'img_src' in p]
        
    else: # 兜底逻辑 (type: 4, 64 等)
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
    """
    分词与自动换行系统，计算文本+表情的包裹坐标。
    增加了自定义表情的左右间距 (padding)。
    """
    if not text:
        return []
        
    lines = []
    emoji_padding = 6  # 左右间距
    total_emoji_w = emoji_size + emoji_padding * 2
    
    # 匹配自定义 B站 emoji 的正则表达式
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
                current_line.append({
                    'type': 'emoji', 
                    'url': emoji_dict[token], 
                    'x': current_x + emoji_padding # 留出左边距
                })
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

# =========================
# 组件渲染函数
# =========================

def render_text_and_images(text, pic_urls, width, font, emoji_dict, text_color):
    """独立渲染文本和配图图层，返回紧凑包裹的 Image 组件（支持宫格布局）"""
    if not text and not pic_urls:
        return None
        
    emoji_size = int(font.size * 1.5)  # 放大表情包
    line_height = int(font.size * 1.6)
    
    # 1. 计算文本高度
    lines = wrap_and_tokenize_text(text, font, width, emoji_dict, emoji_size)
    text_height = len(lines) * line_height if lines else 0
    
    # 2. 处理宫格图片及计算图片区域总高度
    img_spacing = 8  # 宫格图片间距
    imgs_height = 0
    img_layout = []  # 保存元组: (图片对象, 相对x坐标, 相对y坐标)
    pic_count = len(pic_urls)
    
    if pic_count > 0:
        if pic_count == 1:
            # 单图：等比缩放铺满
            img = download_image(pic_urls[0])
            ratio = width / float(img.width)
            new_height = int(img.height * ratio)
            img = img.resize((width, new_height), Image.Resampling.LANCZOS)
            img_layout.append((img, 0, 0))
            imgs_height = new_height
        else:
            # 多图：宫格布局 (2列或3列)
            columns = 2 if pic_count in (2, 4) else 3
            
            # 计算每张正方形图片的边长
            square_size = (width - (columns - 1) * img_spacing) // columns
            rows = math.ceil(pic_count / columns)
            imgs_height = rows * square_size + (rows - 1) * img_spacing
            
            for i, url in enumerate(pic_urls):
                img = download_image(url)
                img = crop_center_square(img)  # 裁剪为正方形
                img = img.resize((square_size, square_size), Image.Resampling.LANCZOS)
                
                row = i // columns
                col = i % columns
                x = col * (square_size + img_spacing)
                y = row * (square_size + img_spacing)
                img_layout.append((img, x, y))
        
    total_height = text_height + imgs_height
    # 如果有文字且有图片，文字和图片之间增加 10px 间距
    if text_height > 0 and imgs_height > 0:
        total_height += 10
        
    if total_height <= 0:
        return None
        
    # 3. 绘制独立画布
    canvas = Image.new("RGBA", (width, total_height), (255, 255, 255, 0))
    draw_y = 0
    
    # 绘制文字
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
                        e_img = download_image(el['url'])
                        e_img = e_img.resize((emoji_size, emoji_size), Image.Resampling.LANCZOS)
                        y_offset = max(0, (line_height - emoji_size) // 2) - 2
                        canvas.paste(e_img, (int(el['x']), int(draw_y + y_offset)), mask=e_img if e_img.mode == 'RGBA' else None)
                draw_y += line_height
                
    # 绘制图片
    if img_layout:
        if text_height > 0:
            draw_y += 10 # 文字与图片的间距
        base_y = draw_y
        for img, x, y in img_layout:
            canvas.paste(img, (int(x), int(base_y + y)), mask=img if img.mode == 'RGBA' else None)
            
    return canvas

def render_bilibili_card(card_json_str, dy_type, orig_type, timestamp, emoji_details=None, width=800):
    """组装完整的动态图片"""
    try:
        data = json.loads(card_json_str)
    except json.JSONDecodeError:
        raise ValueError("传入的不是有效的 JSON 字符串")

    # 1. 整理 Emoji 字典
    emoji_dict = {e['emoji_name']: e['url'] for e in (emoji_details or [])}

    # 2. 提取主体与转发信息
    main_info = extract_dynamic_info(data, dy_type)
    
    origin_info = None
    if 'origin' in data and data['origin'] and data['origin'] != 'null':
        try:
            origin_data = json.loads(data['origin'])
            origin_info = extract_dynamic_info(origin_data, orig_type)
            # 在转发框首部加上被转发者
            origin_info['text'] = f"@{origin_info['username']}: {origin_info['text']}"
        except json.JSONDecodeError:
            pass

    # 3. 样式配置
    margin = 40
    avatar_size = 80
    
    try:
        name_font = ImageFont.truetype(FONT_PATH, 32)
        time_font = ImageFont.truetype(FONT_PATH, 24)
        content_font = ImageFont.truetype(FONT_PATH, 28)
    except IOError:
        raise FileNotFoundError(f"找不到字体: {FONT_PATH}！请将其与脚本放在同一目录。")

    time_str = datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M') if timestamp else "未知时间"
    main_width = width - margin * 2

    # --- 渲染组件 A：头部 ---
    header_height = max(avatar_size, 32 + 24 + 10)
    header_canvas = Image.new("RGBA", (main_width, header_height), (255, 255, 255, 0))
    header_draw = ImageDraw.Draw(header_canvas)
    
    if main_info['avatar_url']:
        avatar_img = download_image(main_info['avatar_url'])
        avatar_img = create_circular_avatar(avatar_img, avatar_size)
        header_canvas.paste(avatar_img, (0, 0), mask=avatar_img)
        
    text_x = avatar_size + 20
    header_draw.text((text_x, 0), main_info['username'], font=name_font, fill=(251, 114, 153, 255))
    header_draw.text((text_x, 42), time_str, font=time_font, fill=(153, 153, 153, 255))

    # --- 渲染组件 B：主体正文 ---
    main_content_canvas = render_text_and_images(
        text=main_info['text'], pic_urls=main_info['pic_urls'],
        width=main_width, font=content_font, emoji_dict=emoji_dict, text_color=(34, 34, 34, 255)
    )

    # --- 渲染组件 C：转发框区 ---
    origin_canvas = None
    if origin_info:
        origin_margin = 24
        origin_inner_width = main_width - origin_margin * 2
        origin_inner_canvas = render_text_and_images(
            text=origin_info['text'], pic_urls=origin_info['pic_urls'],
            width=origin_inner_width, font=content_font, emoji_dict=emoji_dict, text_color=(102, 102, 102, 255)
        )
        if origin_inner_canvas:
            box_height = origin_inner_canvas.height + origin_margin * 2
            origin_canvas = Image.new("RGBA", (main_width, box_height), (255, 255, 255, 0))
            draw_box = ImageDraw.Draw(origin_canvas)
            draw_box.rounded_rectangle((0, 0, main_width, box_height), radius=12, fill=(244, 245, 247, 255))
            origin_canvas.paste(origin_inner_canvas, (origin_margin, origin_margin), origin_inner_canvas)

    # --- 最终拼装画布 ---
    total_y = margin + header_height + 30
    if main_content_canvas:
        total_y += main_content_canvas.height + 20
    if origin_canvas:
        total_y += origin_canvas.height + 20
    total_y += margin - 20 # 底部边缘

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