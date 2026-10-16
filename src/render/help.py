from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, TYPE_CHECKING

from src.common.utils import ROOT

if TYPE_CHECKING:
    from nonebot_plugin_imageutils import BuildImage


def _smart_wrap(text: str, font_size: int, max_width: int, weight: str = "normal") -> str:
    """按英文单词、汉字和完整 Emoji 序列换行。"""
    from src.render.emoji_text import Text2Image

    return (
        Text2Image.from_text(text, font_size, weight=weight)
        .wrap(max_width)
        .wrapped_text
    )


def draw_help_card(sections: list[dict[str, Any]], subtitle: str = "") -> BuildImage:
    from nonebot_plugin_imageutils import BuildImage
    from src.render.emoji_text import Text2Image

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


def get_help_sections() -> list[dict[str, Any]]:
    """帮助图与群聊总结共用的公开命令说明。"""
    return [
        {
            "title": "用户相关",
            "items": [
                ("曾用名 <UID/用户名>", "查询用户的曾用名"),
                ("查牌子 <UID/用户名>", "查询用户当前可确认的粉丝牌"),
                (
                    "查弹幕 <UID/用户名> [日期/数量] [日期/数量]",
                    "查询互动记录，可指定结束日期和/或数量"
                ),
                (
                    "有谁说过 <关键词> [日期/数量] [日期/数量]",
                    "查询截至指定日期、包含关键词的最近发言"
                ),
            ],
        },
        {
            "title": "直播相关",
            "items": [
                ("查SC [UID/用户名/日期]", "查询醒目留言，默认查当日SC"),
                ("查同接 [日期] [直播场次]", "查看直播同接趋势，默认最近直播"),
                (
                    "查盲盒 [日期/月份/年份] [直播场次]",
                    "查看心动盲盒盈亏榜，默认最近直播",
                ),
                ("弹幕榜 [时间范围/日期] [场次/结束日期]", "默认最近直播；支持2026、2026-08、近1月（30天）及起止日期"),
                ("直播记录 [月份]", "查看直播记录，默认当月"),
                ("切片 [内容] [日期时间/时间]", "北京时间，支持YYYY-MM-DD HH:MM[:SS]或HH:MM[:SS]；仅时间未到取前一天，省略则记录当前时间"),
                ("切片文档 [日期] [直播场次]", "查看切片时间轴，默认最近一场"),
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
                ("开播 [标签/全部]", "默认查看 VR、PSP、LDC 开播状态，输入“开播 全部”查看完整列表"),
                ("查看标签 <标签>", "列出指定标签下收录的全部直播间"),
                ("添加直播 <房间号> [标签]", "添加直播间到开播列表，可附加标签"),
                ("删除直播 <房间号>", "从开播列表中删除直播间，仅管理员和添加者可操作"),
                ("添加标签 <房间号> [标签]", "为指定直播间添加标签"),
                ("修改标签 <房间号> [标签]", "为指定直播间修改标签"),
            ], 
        },
        {
            "title": "主播数据",
            "items": [
                ("查粉丝 [天数/日期范围]", "默认1天；支持2025、2025-08及起始日期 结束日期"),
                ("查舰长 [天数/日期范围]", "别名查大航海；默认1天，支持年份、月份及起止日期"),
                ("查粉丝团 [天数/日期范围]", "默认1天；支持年份、月份及起止日期"),
                (
                    "粉丝团 [主播…]",
                    "查看当天粉丝团成员，可填写多名主播获取粉丝团交集",
                ),
                (
                    "斗虫 [社团] [月份/年份] [结束月份]",
                    "查询社团成员营收，填写两个完整月份可汇总区间数据",
                ),
            ],
        },
        {
            "title": "歌曲相关",
            "items": [
                ("查歌曲 <歌名>", "查询歌曲的演唱记录"),
                ("查歌手 <歌手名或别名>", "列出该歌手的全部歌曲与次数"),
                (
                    "查歌单 [日期] [排序方式]",
                    "分页列出曲库，可按歌手、语言、歌名或次数排列",
                ),
                ("随机歌曲 [最少演唱次数]", "随机抽取一首演唱过的歌曲，默认3次"),
                ("在唱什么 [日期时间]", "检索现在（或其他时间）正在唱的歌曲"),
            ],
        },
        {
            "title": "表情制作",
            "items": [
                ("喜报 <内容>", "生成喜报图片"),
                ("悲报 <内容>", "生成悲报图片"),
                ("卖掉了", "回复图片或附图，生成卖掉了表情（静态）"),
            ],
        },
        {
            "title": "杂项",
            "items": [
                ("查缩写 <缩写>", "查询缩写的含义"),
                ("新增缩写 <缩写> <释义>", "添加缩写释义并提交审核"),
                ("删除缩写 <缩写> [释义]", "删除自己添加的缩写释义"),
                ("抉择/choose <选项…>", "随机选择一个选项"),
                ("贴表情榜 / 被贴表情榜 [时间] [结束日期]", "本群表情回应排名，默认本月；支持今天、近7天等"),
                ("新增梗/addmeme <名字> <内容>", "提交审核；回复文本时可仅填写多个空格分隔的标签"),
                ("新增图片/addimage <标签1> [标签2 ...]", "回复图片，加入多个标签并等待管理员审核"),
                ("来点 <名字>", "随机抽取内容，老婆和老公每日不重复"),
                ("在聊什么 [条数/时间]", "默认500条；支持30分钟、2小时、08:00、日期时间或起止时间，时间查询限最近24小时且最多5000条；需先开启"),
            ],
        },
    ]


def save_help_card() -> dict[str, Any]:
    sections = get_help_sections()
    save_path, _ = _get_cache_path("help", sections, "")
    if not Path(save_path).exists():
        canvas = draw_help_card(sections)
        canvas.image.convert("RGB").save(save_path, format="PNG")

    return {"image_path": str(save_path)}


def get_admin_help_sections() -> list[dict[str, Any]]:
    """帮助图与群聊总结共用的管理命令说明。"""
    return [
        {
            "title": "管理员命令",
            "items": [
                ("查看订阅", "查看当前群订阅"),
                ("设置订阅 <房间号>", "设置当前群订阅"),
                ("删除订阅", "删除当前群订阅"),
                ("设置昵称", "修改当前订阅主播的昵称"),
                ("打开功能 <功能>", "开启指定功能"),
                ("关闭功能 <功能>", "关闭指定功能"),
                ("功能状态 <功能>", "查看功能当前状态"),
                ("新增歌手别名 <别名> | <完整歌手名>", "登记查歌手使用的别名"),
                ("本房互动转发 / 外房互动转发", "可分别打开或关闭弹幕、礼物、舰长、SC通知，不影响采集"),
                ("设置CD <功能名> <时间> [次数]", "设置本群每位用户的命令额度，默认1次"),
            ],
        },
    ]


def save_admin_help_card() -> dict[str, Any]:
    sections = get_admin_help_sections()
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
