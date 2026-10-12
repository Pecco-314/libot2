"""历史动态查询与动态卡片生成。"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime

from nonebot.adapters.onebot.v11 import Bot, Event, Message, MessageSegment
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

from src.common.utils import ROOT
from src.db.activity import (
    get_activity_by_date_index,
    list_activities_by_date,
    list_activities_by_month,
)
from src.plugins.command_registry import activity_generate_cmd, activity_list_cmd
from src.plugins.command_support import logger
from src.plugins.utils import get_query_room_id, send_forward_message
from src.render.activity import render_bilibili_card
from src.render.activity_list import render_activity_list


def _parse_activity_month(text: str) -> tuple[int, int] | None:
    value = text.strip()
    if not value:
        now = datetime.now()
        return now.year, now.month

    match = re.fullmatch(r"(20\d{2})-?(0[1-9]|1[0-2])", value)
    if match:
        return int(match.group(1)), int(match.group(2))

    match = re.fullmatch(r"0?([1-9]|1[0-2])", value)
    if match:
        return datetime.now().year, int(match.group(1))
    return None


@activity_list_cmd.handle()
async def handle_activity_list(
    matcher: Matcher,
    bot: Bot,
    event: Event,
    arg=CommandArg(),
):
    room_id = get_query_room_id(event)

    target_month = _parse_activity_month(arg.extract_plain_text())
    if target_month is None:
        await matcher.finish("月份格式错误，请使用如 202607、2026-07 或 7 的格式")
    year, month = target_month
    activities = list_activities_by_month(room_id, year, month)
    if not activities:
        await matcher.finish(f"{year:04d} 年 {month:02d} 月没有动态记录")

    try:
        images = await render_activity_list(
            room_id,
            year,
            month,
            activities,
        )
    except Exception as exc:
        logger.error("渲染动态列表失败: %s", exc)
        await matcher.finish("动态列表渲染失败")

    if len(images) == 1:
        await matcher.finish(MessageSegment.image(file=str(images[0])))

    nodes = [
        {
            "type": "node",
            "data": {
                "name": "LiBot",
                "uin": bot.self_id,
                "content": MessageSegment.image(file=str(image_path)),
            },
        }
        for image_path in images
    ]
    try:
        await send_forward_message(bot, event, nodes)
    except Exception as exc:
        logger.error("发送动态列表合并转发失败: %s", exc)
        await matcher.finish("动态列表发送失败，请稍后再试")


@activity_generate_cmd.handle()
async def handle_activity_generate(
    matcher: Matcher,
    bot: Bot,
    event: Event,
    arg=CommandArg(),
):
    room_id = get_query_room_id(event)

    parts = arg.extract_plain_text().strip().split()
    if len(parts) not in {1, 2}:
        await matcher.finish("用法：/生成动态 <日期> [序号]")
    date_text = parts[0]
    try:
        datetime.strptime(date_text, "%Y-%m-%d")
    except ValueError:
        await matcher.finish("日期格式错误，正确格式：YYYY-MM-DD")

    if len(parts) == 2:
        index_text = parts[1]
        if not index_text.isdigit() or int(index_text) < 1:
            await matcher.finish("序号必须是大于 0 的整数")
        activity = get_activity_by_date_index(
            room_id,
            date_text,
            int(index_text),
        )
        if activity is None:
            await matcher.finish(f"没有找到 {date_text} 的第 {index_text} 条动态")
        selected = [(int(index_text), activity)]
    else:
        activities = list_activities_by_date(room_id, date_text)
        if not activities:
            await matcher.finish(f"{date_text} 没有动态记录")
        selected = list(enumerate(activities, 1))

    nodes = []
    for index, activity in selected:
        safe_id = re.sub(
            r"[^0-9A-Za-z_-]",
            "_",
            str(activity["activity_id"]),
        )
        image_path = ROOT / "data" / "images" / "activity" / f"{safe_id}.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        if not image_path.exists():
            try:
                image = await render_bilibili_card(activity["item"])
                output = image.image.convert("RGB")
                await asyncio.to_thread(
                    output.save,
                    image_path,
                    format="PNG",
                )
            except Exception as exc:
                logger.error(
                    "生成历史动态失败 activity_id=%s: %s",
                    activity["activity_id"],
                    exc,
                )
                await matcher.finish("历史动态图片生成失败")

        nodes.append(
            {
                "type": "node",
                "data": {
                    "name": "LiBot",
                    "uin": bot.self_id,
                    "content": Message(
                        [
                            MessageSegment.text(f"{date_text} #{index}\n"),
                            MessageSegment.image(file=str(image_path)),
                        ]
                    ),
                },
            }
        )

    try:
        await send_forward_message(bot, event, nodes)
    except Exception as exc:
        logger.error("发送历史动态合并转发失败: %s", exc)
        await matcher.finish("历史动态发送失败，请稍后再试")
