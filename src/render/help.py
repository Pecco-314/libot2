import hashlib
import json
from pathlib import Path
from typing import Any

from nonebot_plugin_imageutils import BuildImage
from src.render.emoji_text import Text2Image

from src.common.utils import ROOT


def _smart_wrap(text: str, font_size: int, max_width: int, weight: str = "normal") -> str:
    """按英文单词、汉字和完整 Emoji 序列换行。"""
    return (
        Text2Image.from_text(text, font_size, weight=weight)
        .wrap(max_width)
        .wrapped_text
    )


def draw_help_card(sections: list[dict[str, Any]], subtitle: str = "") -> BuildImage:
    width = 760
    padding = 40
    content_width = width - padding * 2
    bg_color = (255, 255, 255, 255)

    title_t2i = Text2Image.from_text("LiBot 指令帮助", 42, weight="bold", fill=(34, 34, 34))
    subtitle_t2i = Text2Image.from_text(subtitle, 22, fill=(120, 120, 120)) if subtitle else None
    row_items: list[dict[str, Any]] = []

    cmd_col_width = 288
    gap = 16
    desc_max_width = content_width - cmd_col_width - gap

    for section in sections:
        section_title = Text2Image.from_text(section["title"], 28, weight="bold", fill=(34, 34, 34))

        rows = []
        for cmd, desc in section["items"]:
            cmd_text = _smart_wrap(cmd, 24, cmd_col_width, weight="bold")
            cmd_img = Text2Image.from_text(cmd_text, 24, weight="bold", fill=(0, 102, 204))

            desc_text = _smart_wrap(desc, 22, desc_max_width)
            desc_img = Text2Image.from_text(desc_text, 22, fill=(80, 80, 80))

            row_height = max(cmd_img.height, desc_img.height)
            rows.append({
                "cmd": cmd_img,
                "desc": desc_img,
                "height": row_height,
            })
        row_items.append({
            "title": section_title,
            "rows": rows,
        })

    content_h = (
        padding +
        title_t2i.height + 10 +
        (subtitle_t2i.height + 24 if subtitle_t2i else 0) +
        sum(
            item["title"].height + 12 + sum(r["height"] + 12 for r in item["rows"]) + 16
            for item in row_items
        ) +
        padding
    )

    canvas = BuildImage.new("RGBA", (width, int(content_h)), bg_color)

    curr_y = padding
    title_t2i.draw_on_image(canvas.image, (padding, curr_y))
    curr_y += title_t2i.height + 10
    if subtitle_t2i:
        subtitle_t2i.draw_on_image(canvas.image, (padding, curr_y))
        curr_y += subtitle_t2i.height + 24

    for item in row_items:
        item["title"].draw_on_image(canvas.image, (padding, curr_y))
        curr_y += item["title"].height + 12

        for row in item["rows"]:
            row["cmd"].draw_on_image(canvas.image, (padding, curr_y))
            row["desc"].draw_on_image(canvas.image, (padding + cmd_col_width + gap, curr_y))
            curr_y += row["height"] + 12
        curr_y += 16

    return canvas


def _get_cache_path(prefix: str, sections: list[dict[str, Any]], subtitle: str) -> tuple[str, str]:
    payload = json.dumps({"subtitle": subtitle, "sections": sections}, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    file_name = f"{prefix}_{digest}.png"
    save_dir = ROOT / "data" / "images" / "help"
    save_dir.mkdir(parents=True, exist_ok=True)
    return str(save_dir / file_name), digest


def save_help_card() -> dict[str, Any]:
    sections = [
        {
            "title": "用户相关",
            "items": [
                ("曾用名 <UID/用户名>", "查询用户的曾用名"),
                (
                    "查弹幕 <UID/用户名> [日期/数量] [日期/数量]",
                    "查询互动记录，可指定结束日期和/或数量"
                ),
                (
                    "有谁说过 <关键词> [数量]",
                    "查询包含关键词的发言，默认100条，最多2000条"
                ),
            ],
        },
        {
            "title": "直播相关",
            "items": [
                ("查SC [UID/用户名/日期]", "查询醒目留言，默认查当日SC"),
                ("查同接 [日期] [直播场次]", "查看直播同接趋势，默认最近直播"),
                ("弹幕榜 [日期] [直播场次]", "查看直播互动榜单，默认最近直播"),
                ("直播记录 [月份]", "查看直播记录，默认当月"),
            ],
        },
        {
            "title": "动态相关",
            "items": [
                ("查动态 [月份]", "列出指定月份的动态概要，默认本月"),
                (
                    "生成动态 <日期> [序号]",
                    "生成当天全部或指定序号的历史动态图片",
                ),
            ],
        },
        {
            "title": "开播列表",
            "items": [
                ("开播列表 [标签]", "查看开播列表的各个直播间的开播状态，可指定标签"),
                ("添加直播 <房间号> [标签]", "添加直播间到开播列表，可附加标签"),
                ("删除直播 <房间号>", "从开播列表中删除直播间，仅管理员和添加者可操作"),
                ("添加标签 <房间号> [标签]", "为指定直播间添加标签"),
                ("修改标签 <房间号> [标签]", "为指定直播间修改标签"),
            ], 
        },
        {
            "title": "主播数据",
            "items": [
                ("查粉丝 [天数]", "查询订阅主播粉丝数趋势，默认1天"),
                ("查舰长 [天数]", "查询订阅主播大航海数趋势，默认1天"),
                ("查粉丝团 [天数]", "查询订阅主播粉丝团人数趋势，默认1天"),
                ("斗虫 [社团] [月份/年份]", "查询社团成员营收数据，默认VR当月"),
            ],
        },
        {
            "title": "歌曲相关",
            "items": [
                ("查歌曲 <歌名>", "查询歌曲的演唱记录"),
                ("查歌手 <歌手名>", "列出该歌手的全部歌曲与次数"),
                ("查歌单 <日期>", "列出某日期的歌单"),
                ("随机歌曲 [最少演唱次数]", "随机抽取一首演唱过的歌曲，默认3次"),
                ("在唱什么 [日期时间]", "检索现在（或其他时间）正在唱的歌曲"),
            ],
        },
    ]

    save_path, _ = _get_cache_path("help", sections, "")
    if not Path(save_path).exists():
        canvas = draw_help_card(sections)
        canvas.image.convert("RGB").save(save_path, format="PNG")

    return {"image_path": str(save_path)}


def save_admin_help_card() -> dict[str, Any]:
    sections = [
        {
            "title": "管理员命令",
            "items": [
                ("查看管理员", "查看当前群管理员"),
                ("添加管理员 <QQ号>", "添加群管理员"),
                ("删除管理员 <QQ号>", "删除群管理员"),
                ("查看订阅", "查看当前群订阅"),
                ("设置订阅 <房间号>", "设置当前群订阅"),
                ("删除订阅", "删除当前群订阅"),
                ("设置昵称", "修改当前订阅主播的昵称"),
                ("打开功能 <功能>", "开启指定功能"),
                ("关闭功能 <功能>", "关闭指定功能"),
                ("功能状态 <功能>", "查看功能当前状态"),
            ],
        },
    ]

    subtitle = "仅管理员可用"
    save_path, _ = _get_cache_path("admin_help", sections, subtitle)
    if not Path(save_path).exists():
        canvas = draw_help_card(sections, subtitle)
        canvas.image.convert("RGB").save(save_path, format="PNG")

    return {"image_path": str(save_path)}


async def render_help_image() -> dict[str, Any]:
    return save_help_card()


async def render_admin_help_image() -> dict[str, Any]:
    return save_admin_help_card()
