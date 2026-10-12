"""群聊摘要查询和合并转发。"""

from __future__ import annotations

import asyncio

from nonebot.adapters.onebot.v11 import Bot, Event, MessageSegment
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

from src.common.chat_summary_service import count_summary, parse_summary_count
from src.db.command_config import get_command_config
from src.plugins.command_registry import chat_summary_cmd
from src.plugins.command_support import logger
from src.plugins.utils import get_group_id, send_forward_message
from src.render.chat_summary import render_summary_images

_CHAT_SUMMARY_BUSY: set[int] = set()


_CHAT_SUMMARY_SLOTS = asyncio.Semaphore(2)


@chat_summary_cmd.handle()
async def handle_chat_summary(
    matcher: Matcher, bot: Bot, event: Event, arg=CommandArg()
):
    group_id = get_group_id(event)
    if group_id is None:
        await matcher.finish("请在要总结的群聊中使用")
    # 涉及外部模型请求，超级管理员也必须先显式开启本群功能。
    config = await asyncio.to_thread(get_command_config, group_id, "在聊什么")
    if not config.enabled:
        await matcher.finish("本群未开启此功能，请先使用：打开功能 在聊什么")
    try:
        count = parse_summary_count(arg.extract_plain_text())
    except ValueError as exc:
        await matcher.finish(str(exc))
    if group_id in _CHAT_SUMMARY_BUSY:
        await matcher.finish("这个群正在总结，请稍等")
    _CHAT_SUMMARY_BUSY.add(group_id)
    try:

        async def notify_generating():
            try:
                await matcher.send("正在总结中，请稍等……")
            except Exception as exc:
                logger.warning(
                    "群聊总结进度提示发送失败 group=%s error=%s",
                    group_id,
                    type(exc).__name__,
                )

        async with _CHAT_SUMMARY_SLOTS:
            result = await count_summary(
                group_id, count, bot_qq=bot.self_id, on_generate=notify_generating
            )
            paths = await render_summary_images(result)
    except Exception as exc:
        logger.warning("群聊总结失败 group=%s error=%s", group_id, type(exc).__name__)
        detail = (
            str(exc)
            if isinstance(exc, (ValueError, RuntimeError))
            else "总结暂时不可用，请稍后再试"
        )
        await matcher.finish(f"总结失败：{detail}")
    finally:
        _CHAT_SUMMARY_BUSY.discard(group_id)
    nodes = [
        {
            "type": "node",
            "data": {
                "name": "Libot",
                "uin": bot.self_id,
                "content": MessageSegment.image(file=str(path)),
            },
        }
        for path in paths
    ]
    await send_forward_message(bot, event, nodes)
