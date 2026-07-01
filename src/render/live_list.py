import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from nonebot_plugin_imageutils import BuildImage, Text2Image

from src.common.utils import ROOT, truncate_name
from src.spider.api import get_room_info, get_master_info
from src.db.liver import get_name_by_uid
from src.db.live_list import update_live_list_uname

from PIL import ImageDraw, ImageFont
from nonebot_plugin_imageutils import BuildImage, Text2Image

logger = logging.getLogger("render.live_list")

async def get_live_data(room_infos: list[dict]) -> list[dict[str, Any]]:
    result = []
    now = datetime.now()
    
    seen_real_rooms = set()

    async def fetch(item: dict):
        room_id = item["room_id"]
        db_uname = item.get("uname")
        
        try:
            resp = await get_room_info(room_id)
            if not resp.get("ok"):
                return None
            data = resp.get("body", {}).get("data", {})
            if not data or data.get("live_status") != 1:
                return None
            
            real_room_id = int(data.get("room_id", room_id))
            uid = int(data.get("uid", 0))
            
            if real_room_id in seen_real_rooms:
                return None
            seen_real_rooms.add(real_room_id)
            
            title = data.get("title", "")
            live_time_str = data.get("live_time", "")
            try:
                live_time = datetime.strptime(live_time_str, "%Y-%m-%d %H:%M:%S")
                duration = now - live_time
                seconds = max(int(duration.total_seconds()), 0)
                duration_str = f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"
            except Exception:
                duration_str = "未知"
                seconds = 0
            
            if not db_uname:
                fetched_name = get_name_by_uid(uid)
                if not fetched_name and uid:
                    try:
                        m_resp = await get_master_info(uid)
                        if m_resp.get("ok"):
                            fetched_name = m_resp["body"]["data"]["info"]["uname"]
                    except Exception:
                        pass
                
                db_uname = fetched_name or str(real_room_id)
                try:
                    update_live_list_uname(room_id, db_uname)
                except Exception as e:
                    logger.warning(f"自动补全写入直播间名称失败: {e}")
            
            return {
                "room_id": real_room_id,
                "uname": db_uname,
                "title": title,
                "duration": duration_str,
                "seconds": seconds
            }
        except Exception as e:
            logger.error(f"获取直播间 {room_id} 状态失败: {e}")
            return None

    tasks = [fetch(info) for info in room_infos]
    results = await asyncio.gather(*tasks)
    
    for res in results:
        if res is not None:
            result.append(res)
            
    result.sort(key=lambda x: x["seconds"], reverse=True)
    return result


async def render_live_list_image(room_infos: list[dict], filter_tag: str | None = None) -> Path | None:
    data_list = await get_live_data(room_infos)
    if not data_list:
        return None

    save_dir = ROOT / "data" / "images" / "live_list"
    save_dir.mkdir(parents=True, exist_ok=True)
    
    # 顶部大标题（使用 Text2Image 没关系，因为只执行一次）
    title_text = f"开播列表 - {filter_tag}" if filter_tag else "开播列表"
    title_t2i = Text2Image.from_text(title_text, 36, weight="bold", fill=(34, 34, 34))

    cols = [
        ("主播名称", 250),
        ("直播间标题", 450),
        ("已播时长", 150),
    ]
    
    padding = 40
    width = sum(c[1] for c in cols) + padding * 2
    row_h = 50
    header_h = 60
    
    height = (
        padding + 
        title_t2i.height + 20 + 
        header_h + 
        len(data_list) * row_h + 
        padding
    )
    
    canvas = BuildImage.new("RGBA", (width, height), (255, 255, 255, 255))
    y = padding
    
    # 1. 绘制顶部大标题
    title_t2i.draw_on_image(canvas.image, (padding, y))
    y += title_t2i.height + 20
    
    x_offset = padding
    
    # 2. 绘制表头 (只执行 3 次，保留 Text2Image 影响不大)
    for col_name, col_w in cols:
        t2i = Text2Image.from_text(col_name, 28, weight="bold", fill=(50, 50, 50))
        t2i.draw_on_image(canvas.image, (x_offset, y))
        x_offset += col_w
    
    y += header_h
    
    font_path = str(ROOT / "fonts" / "NotoSansCJKsc-Regular.otf")
    
    try:
        font_normal = ImageFont.truetype(font_path, 26)
        # 如果你没有专门的粗体文件，就继续用常规字体；如果有，可以加载为 font_bold
        font_bold = font_normal 
    except OSError:
        logger.warning(f"无法加载字体文件: {font_path}，使用默认字体")
        font_normal = ImageFont.load_default()
        font_bold = ImageFont.load_default()

    # 提取 PIL 原生的画笔工具
    draw = ImageDraw.Draw(canvas.image)

    # 3. 绘制列表行数据 (高频循环，全部切为原生绘制)
    for item in data_list:
        x_offset = padding
        
        name = truncate_name(item["uname"], max_len=24)
        # 耗时毫秒级：跳过分词和回退，直接写像素
        draw.text((x_offset, y), name, font=font_normal, fill=(30, 30, 30))
        x_offset += cols[0][1]
        
        title = truncate_name(item["title"], max_len=34)
        draw.text((x_offset, y), title, font=font_bold, fill=(30, 30, 30))
        x_offset += cols[1][1]
        
        draw.text((x_offset, y), item["duration"], font=font_normal, fill=(80, 80, 80))
        
        y += row_h

    # 保存图片
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    out_path = save_dir / f"live_list_global_{timestamp}.png"
    canvas.image.save(out_path)
    
    return out_path