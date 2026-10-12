"""群命令开关与滑动窗口冷却策略。"""

from __future__ import annotations

import asyncio
import os
import re
import time

from nonebot.adapters.onebot.v11 import Bot, Event, Message, MessageSegment
from nonebot.matcher import Matcher
from nonebot.message import run_preprocessor
from nonebot.params import Command
from nonebot.permission import SUPERUSER

from src.db.command_config import CommandConfig, get_command_config
from src.plugins.command_rate_limit import CommandRateLimiter
from src.plugins.command_registry import COMMAND_MODULE_NAME, _canonical_command_name
from src.plugins.utils import get_group_id

_COMMAND_CONFIG_CACHE: dict[tuple[int, str], CommandConfig] = {}


_COMMAND_RATE_LIMITER = CommandRateLimiter()


def _parse_duration(text: str) -> float | None:
    normalized = re.sub(r"\s+", "", text).casefold()
    if normalized in {"无限制", "不限", "关闭", "取消", "0", "0秒"}:
        return 0.0
    if not re.fullmatch(r"(?:\d+(?:\.\d+)?(?:秒|分钟|分|小时|时|天))+", normalized):
        return None

    unit_seconds = {
        "秒": 1,
        "分钟": 60,
        "分": 60,
        "小时": 3600,
        "时": 3600,
        "天": 86400,
    }
    seconds = sum(
        float(value) * unit_seconds[unit]
        for value, unit in re.findall(
            r"(\d+(?:\.\d+)?)(分钟|小时|秒|分|时|天)", normalized
        )
    )
    return seconds if seconds > 0 else None


def _format_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:g}秒"
    if seconds < 3600 and seconds % 60 == 0:
        return f"{seconds / 60:g}分钟"
    if seconds < 86400 and seconds % 3600 == 0:
        return f"{seconds / 3600:g}小时"
    if seconds % 86400 == 0:
        return f"{seconds / 86400:g}天"
    return f"{seconds:g}秒"


@run_preprocessor
async def apply_configured_command_cooldown(
    matcher: Matcher,
    bot: Bot,
    event: Event,
    command: tuple[str, ...] = Command(),
) -> None:
    # 仅命令注册模块拥有的 matcher 计入 CD，通用消息处理器共享事件但不消耗额度。
    if matcher.module_name != COMMAND_MODULE_NAME:
        return
    group_id = get_group_id(event)
    if group_id is None or not command:
        return
    canonical_name = _canonical_command_name(command[0])
    if canonical_name is None:
        return
    # 项目管理员与 NoneBot 超级管理员均不受群命令开关和 CD 限制。
    manager_qq = os.environ.get("MANAGER_QQ", "").strip()
    if (manager_qq and event.get_user_id() == manager_qq) or await SUPERUSER(
        bot, event
    ):
        return

    setting_key = (group_id, canonical_name)
    if setting_key not in _COMMAND_CONFIG_CACHE:
        _COMMAND_CONFIG_CACHE[setting_key] = await asyncio.to_thread(
            get_command_config,
            group_id,
            canonical_name,
        )
    config = _COMMAND_CONFIG_CACHE[setting_key]
    if config.enabled and config.cooldown_seconds is None:
        return
    if not config.enabled:
        await matcher.finish("本群未开启此功能")

    seconds = config.cooldown_seconds
    if seconds is None:
        return
    user_id = int(event.get_user_id())
    usage_key = (group_id, canonical_name, user_id)
    now = time.monotonic()
    allowed, notify = _COMMAND_RATE_LIMITER.check(
        usage_key, seconds, config.cooldown_count, now
    )
    if not allowed and notify:
        await matcher.finish(
            Message(
                [
                    MessageSegment.at(user_id),
                    MessageSegment.text(
                        f" CD中（当前CD：{_format_duration(seconds)}{config.cooldown_count}次）"
                    ),
                ]
            )
        )
    if not allowed:
        await matcher.finish()
