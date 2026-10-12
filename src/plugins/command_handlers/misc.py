"""帮助、抉择和表情回应榜命令。"""

from __future__ import annotations

import random

from nonebot.adapters.onebot.v11 import Bot, Event, Message, MessageSegment
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

from src.plugins.command_registry import (
    choose_cmd,
    help_cmd,
    reaction_rank_cmd,
    reaction_received_rank_cmd,
)
from src.plugins.command_support import logger
from src.render.help import render_help_image


@reaction_rank_cmd.handle()
async def handle_reaction_rank(
    bot: Bot, event: Event, matcher: Matcher, arg: Message = CommandArg()
):
    from src.plugins.qq_reactions import show_rank

    await show_rank(bot, event, matcher, arg.extract_plain_text().strip(), False)


@reaction_received_rank_cmd.handle()
async def handle_reaction_received_rank(
    bot: Bot, event: Event, matcher: Matcher, arg: Message = CommandArg()
):
    from src.plugins.qq_reactions import show_rank

    await show_rank(bot, event, matcher, arg.extract_plain_text().strip(), True)


_MIT3URI_CHOOSE_OPTIONS = frozenset(
    option.casefold()
    for option in (
        "三理",
        "三理理",
        "理理",
        "理神",
        "三理Mit3uri",
        "Mit3uri",
        "三理蜜籽粒",
        "蜜籽粒",
    )
)


def _choose_reply(options: list[str]) -> str:
    if any(option.casefold() in _MIT3URI_CHOOSE_OPTIONS for option in options):
        return "永远效忠三理"
    return f"我觉得{random.choice(options)}不错"


@help_cmd.handle()
async def handle_help(matcher: Matcher):
    try:
        result = await render_help_image()
    except Exception as exc:
        logger.error("渲染帮助图片失败: %s", exc)
        await matcher.finish("图片渲染失败")

    await matcher.finish(MessageSegment.image(file=str(result["image_path"])))


@choose_cmd.handle()
async def handle_choose(matcher: Matcher, arg=CommandArg()):
    options = arg.extract_plain_text().split()
    if len(options) < 2:
        await matcher.finish("用法：/抉择 <选项1> <选项2> [更多选项…]")
    await matcher.finish(_choose_reply(options))
