import json
import re
import math
import httpx
from io import BytesIO
from datetime import datetime
from typing import Dict, Optional

from nonebot.log import logger
from nonebot_plugin_imageutils import BuildImage, Text2Image

# 全局图片缓存
IMG_CACHE: Dict[str, BuildImage] = {}

# B 站图片后缀
SUFFIX_AVATAR = "120w_120h_1e_1c.webp"
SUFFIX_GRID = "400w_400h_1e_1c.webp"
SUFFIX_SINGLE = "1080w.webp"

def get_bili_optimized_url(url: str, suffix: str) -> str:
    if not url or "hdslb.com" not in url:
        return url
    return f"{url.split('@')[0]}@{suffix}"

async def download_image(url: str, suffix: str | None = None) -> BuildImage:
    """异步下载并缓存图片，返回 BuildImage 对象"""
    if not url:
        return BuildImage.new("RGBA", (100, 100), (200, 200, 200, 255))
    
    url = 'https:' + url if url.startswith('//') else url
    target_url = get_bili_optimized_url(url, suffix) if suffix else url
    
    if target_url in IMG_CACHE:
        return IMG_CACHE[target_url].copy()
        
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://t.bilibili.com/"}
            resp = await client.get(target_url, headers=headers)
            resp.raise_for_status()
            img = BuildImage.open(BytesIO(resp.content)).convert("RGBA")
            IMG_CACHE[target_url] = img
            return img.copy()
    except Exception as e:
        logger.warning(f"下载图片失败 {target_url}: {e}")
        return BuildImage.new("RGBA", (100, 100), (200, 200, 200, 255))

def extract_dynamic_info(data_dict: dict, dy_type: int) -> dict:
    # (保持原有的提取逻辑不变...)
    # 为了简洁，这里省略提取逻辑的重复部分，仅保留结构
    res = {'username': '未知', 'avatar_url': '', 'text': '', 'pic_urls': []}
    if dy_type == 8: # 视频
        owner = data_dict.get('owner', {})
        res.update({'username': owner.get('name', '未知'), 'avatar_url': owner.get('face', ''), 
                    'text': f"▶ 视频：{data_dict.get('title', '')}", 'pic_urls': [data_dict.get('pic', '')]})
    elif dy_type == 1: # 转发
        user = data_dict.get('user', {})
        res.update({'username': user.get('uname', '未知'), 'avatar_url': user.get('face', ''), 'text': data_dict.get('item', {}).get('content', '')})
    else: # 图文/其他
        user = data_dict.get('user') or data_dict.get('owner') or {}
        item = data_dict.get('item', {})
        res.update({
            'username': user.get('name') or user.get('uname') or "未知",
            'avatar_url': user.get('head_url') or user.get('face') or "",
            'text': item.get('description') or item.get('content') or data_dict.get('dynamic') or "",
            # 核心修复点：使用 or [] 来确保 None 也能转为空列表
            'pic_urls': [p.get('img_src') for p in (item.get('pictures') or [])]
        })
    return res

async def render_text_and_images(text: str, pic_urls: list, width: int, font_size: int, emoji_dict: dict, text_color: tuple, bg_color: tuple) -> Optional[BuildImage]:
    """渲染正文：处理文字、自定义表情和配图"""
    if not text and not pic_urls:
        return None

    # 预估行高和布局
    line_height = int(font_size * 1.5)
    emoji_size = int(font_size * 1.4)
    
    # 构建文字和表情的分块逻辑
    if emoji_dict:
        pattern = re.compile('(' + '|'.join(map(re.escape, emoji_dict.keys())) + ')')
        raw_tokens = [t for t in pattern.split(text) if t] if text else []
    else:
        raw_tokens = [text] if text else []

    lines = []
    curr_line = []
    curr_x = 0

    char_width_cache = {}
    
    for token in raw_tokens:
        if emoji_dict and token in emoji_dict:
            w = emoji_size + 8
            if curr_x + w > width and curr_line:
                lines.append(curr_line)
                curr_line = []
                curr_x = 0
            curr_line.append({'type': 'emoji', 'url': emoji_dict[token], 'w': w, 'x': curr_x})
            curr_x += w
        else:
            for char in token:
                if char == '\n':
                    lines.append(curr_line)
                    curr_line = []
                    curr_x = 0
                    continue

                if char not in char_width_cache:
                    char_width_cache[char] = Text2Image.from_text(char, font_size).width
                char_w = char_width_cache[char]
                
                if curr_x + char_w > width and curr_line:
                    lines.append(curr_line)
                    curr_line = []
                    curr_x = 0
                    
                if curr_line and curr_line[-1]['type'] == 'text':
                    curr_line[-1]['content'] += char
                else:
                    curr_line.append({'type': 'text', 'content': char, 'x': curr_x})
                curr_x += char_w
                
    if curr_line:
        lines.append(curr_line)

    text_h = len(lines) * line_height
    
    # 计算配图高度
    img_h = 0
    img_layout = []
    if pic_urls:
        spacing = 8
        count = len(pic_urls)
        if count == 1:
            p_img = await download_image(pic_urls[0], SUFFIX_SINGLE)
            ratio = width / p_img.width
            p_img = p_img.resize((width, int(p_img.height * ratio)))
            img_layout.append((p_img, 0, 0))
            img_h = p_img.height
        else:
            cols = 2 if count in (2, 4) else 3
            sz = (width - (cols-1)*spacing) // cols
            rows = math.ceil(count / cols)
            img_h = rows * sz + (rows-1)*spacing
            for i, u in enumerate(pic_urls):
                p_img = (await download_image(u, SUFFIX_GRID)).square().resize((sz, sz))
                img_layout.append((p_img, (i%cols)*(sz+spacing), (i//cols)*(sz+spacing)))

    canvas_h = text_h + img_h + (20 if text_h and img_h else 0)
    # 创建带有实心背景的画布
    canvas = BuildImage.new("RGBA", (width, canvas_h), bg_color)

    # 绘制文字
    y = 0
    for line in lines:
        # 即使 line 是因为纯换行产生的空列表，内层循环会跳过，但外层依旧会加上 line_height，完美实现空行占位
        for el in line:
            if el['type'] == 'text':
                t2i = Text2Image.from_text(el['content'], font_size, fill=text_color)
                t2i.draw_on_image(canvas.image, (el['x'], y))
            else:
                e_img = (await download_image(el['url'])).resize((emoji_size, emoji_size))
                canvas.paste(e_img, (int(el['x']), int(y + (line_height-emoji_size)//2)), alpha=True)
        y += line_height

    # 绘制图片
    if img_layout:
        base_y = y + 15 if text_h else 0
        for img, ix, iy in img_layout:
            canvas.paste(img, (int(ix), int(base_y + iy)), alpha=True)
            
    return canvas

async def render_bilibili_card(card_json: str, dy_type: int, orig_type: int, timestamp: int, emoji_details: list = None) -> BuildImage:
    data = json.loads(card_json)
    main_info = extract_dynamic_info(data, dy_type)
    emoji_dict = {e['emoji_name']: e['url'] for e in (emoji_details or [])}
    
    # 准备画布尺寸
    width, margin = 800, 40
    main_w = width - margin * 2
    bg_color = (255, 255, 255, 255)

    # 1. 头部 (头像+名字)
    header = BuildImage.new("RGBA", (main_w, 100), bg_color)
    if main_info['avatar_url']:
        av = (await download_image(main_info['avatar_url'], SUFFIX_AVATAR)).circle().resize((80, 80))
        header.paste(av, (0, 10), alpha=True)
    
    # 名字使用 Text2Image，自动处理 B 站名字里的特殊符号
    Text2Image.from_text(main_info['username'], 32, fill=(251, 114, 153)).draw_on_image(header.image, (100, 15))
    time_str = datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M') if timestamp else ""
    Text2Image.from_text(time_str, 22, fill=(153, 153, 153)).draw_on_image(header.image, (100, 55))

    # 2. 正文
    content_canvas = await render_text_and_images(main_info['text'], main_info['pic_urls'], main_w, 28, emoji_dict, (34, 34, 34), bg_color)

    # 3. 转发内容
    origin_canvas = None
    if 'origin' in data and data['origin'] and data['origin'] != 'null':
        o_data = json.loads(data['origin'])
        o_info = extract_dynamic_info(o_data, orig_type)
        o_info['text'] = f"@{o_info['username']}: {o_info['text']}"
        o_bg = (244, 245, 247, 255)
        o_inner_w = main_w - 40
        origin_inner = await render_text_and_images(o_info['text'], o_info['pic_urls'], o_inner_w, 26, emoji_dict, (102, 102, 102), o_bg)
        if origin_inner:
            origin_canvas = BuildImage.new("RGBA", (main_w, origin_inner.height + 40), bg_color)
            origin_canvas.draw_rounded_rectangle((0, 0, main_w, origin_canvas.height), radius=12, fill=o_bg)
            origin_canvas.paste(origin_inner, (20, 20), alpha=True)

    # 4. 组装
    h_total = margin + 100 + 20
    if content_canvas:
        h_total += content_canvas.height + 20
    if origin_canvas:
        h_total += origin_canvas.height + 20
    h_total += margin

    final = BuildImage.new("RGBA", (width, h_total), bg_color)
    y = margin
    final.paste(header, (margin, y), alpha=True)
    y += 120
    if content_canvas:
        final.paste(content_canvas, (margin, y), alpha=True)
        y += content_canvas.height + 20
    if origin_canvas:
        final.paste(origin_canvas, (margin, y), alpha=True)

    return final