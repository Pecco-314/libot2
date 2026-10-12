"""群功能开关、冷却与订阅配置。"""

from __future__ import annotations

import asyncio
import re

from nonebot.adapters.onebot.v11 import Event, Message, MessageSegment
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

from src.common.push_features import PUSH_FEATURE_ALIASES
from src.db.command_config import (
    get_command_config,
    set_command_cooldown,
    set_command_enabled,
)
from src.db.liver import upsert_liver
from src.db.subscription import (
    get_subscription,
    get_subscription_feature,
    remove_subscription,
    set_subscription,
    set_subscription_feature,
)
from src.plugins.command_policy import (
    _COMMAND_CONFIG_CACHE,
    _COMMAND_RATE_LIMITER,
    _format_duration,
    _parse_duration,
)
from src.plugins.command_registry import (
    _canonical_command_name,
    cooldown_set_cmd,
    feature_disable_cmd,
    feature_enable_cmd,
    feature_status_cmd,
    manager_help_cmd,
    nickname_set_cmd,
    sub_remove_cmd,
    sub_set_cmd,
    sub_show_cmd,
)
from src.plugins.command_support import logger
from src.plugins.utils import (
    _format_name,
    _parse_room_id,
    get_group_id,
    group_manager_required,
)
from src.render.help import render_admin_help_image


@manager_help_cmd.handle()
@group_manager_required
async def handle_manager_help(matcher: Matcher, event: Event):
    try:
        result = await render_admin_help_image()
    except Exception as exc:
        logger.error("渲染管理员帮助图片失败: %s", exc)
        await matcher.finish("图片渲染失败")

    message = Message(
        [
            MessageSegment.text("管理员帮助："),
            MessageSegment.image(file=str(result["image_path"])),
        ]
    )
    await matcher.finish(message)


def _parse_feature_name(text: str) -> str | None:
    normalized = text.strip()
    if not normalized:
        return None
    if normalized in {"测试", "功能测试"}:
        return "测试"
    if normalized in {"艾特全体", "@全体"}:
        return "艾特全体"
    if normalized in {"退群通知", "退群提醒"}:
        return "退群通知"
    if normalized in {"进群欢迎", "进群通知", "欢迎"}:
        return "进群欢迎"
    if normalized in PUSH_FEATURE_ALIASES:
        return PUSH_FEATURE_ALIASES[normalized]
    if normalized in RESERVED_FEATURE_REGISTRY:
        return normalized
    return _canonical_command_name(normalized)


RESERVED_FEATURE_REGISTRY = {
    "测试": {"col": "dev", "name": "测试功能"},
    "艾特全体": {"col": "mention_all", "name": "开播通知艾特全体"},
    "退群通知": {"col": "leave_notice", "name": "退群通知"},
    "进群欢迎": {"col": "join_notice", "name": "进群欢迎"},
    "听歌识曲": {"col": "enable_asr", "name": "听歌识曲"},
}


async def _handle_feature_toggle(
    matcher: Matcher, event: Event, feature: str, enabled: bool
):
    group_id = get_group_id(event)
    if group_id is None:
        await matcher.finish("请在群聊中使用该命令")

    if feature in RESERVED_FEATURE_REGISTRY:
        config = RESERVED_FEATURE_REGISTRY[feature]
        feature_name = config["name"]
        ok = set_subscription_feature(group_id, config["col"], enabled)
        if not ok:
            await matcher.finish(
                f"请先设置订阅，再{'开启' if enabled else '关闭'}{feature_name}"
            )
        await matcher.finish(f"已{'开启' if enabled else '关闭'}{feature_name}")

    await asyncio.to_thread(
        set_command_enabled,
        group_id,
        feature,
        enabled,
        int(event.get_user_id()),
    )
    config = await asyncio.to_thread(get_command_config, group_id, feature)
    _COMMAND_CONFIG_CACHE[(group_id, feature)] = config
    await matcher.finish(f"已{'开启' if enabled else '关闭'}本群「{feature}」功能")


async def _handle_feature_status(matcher: Matcher, event: Event, feature: str):
    group_id = get_group_id(event)
    if group_id is None:
        await matcher.finish("请在群聊中使用该命令")

    if feature in RESERVED_FEATURE_REGISTRY:
        config = RESERVED_FEATURE_REGISTRY[feature]
        is_enabled = get_subscription_feature(group_id, config["col"])
        status = "已开启" if is_enabled else "已关闭"
        await matcher.finish(f"{config['name']}：{status}")

    config = await asyncio.to_thread(get_command_config, group_id, feature)
    _COMMAND_CONFIG_CACHE[(group_id, feature)] = config
    status = "已开启" if config.enabled else "已关闭"
    if feature in PUSH_FEATURE_ALIASES:
        await matcher.finish(f"{feature}：{status}")
    cooldown = (
        f"{_format_duration(config.cooldown_seconds)}{config.cooldown_count}次"
        if config.cooldown_seconds is not None
        else "无限制"
    )
    await matcher.finish(f"{feature}：{status}，CD {cooldown}")


@feature_enable_cmd.handle()
@group_manager_required
async def handle_feature_enable(matcher: Matcher, event: Event, arg=CommandArg()):
    requested = arg.extract_plain_text().strip()
    feature = _parse_feature_name(requested)
    if not feature:
        if requested:
            await matcher.finish(f"没有找到功能：{requested}")
        await matcher.finish("用法：/打开功能 <功能>")
    await _handle_feature_toggle(matcher, event, feature, True)


@feature_disable_cmd.handle()
@group_manager_required
async def handle_feature_disable(matcher: Matcher, event: Event, arg=CommandArg()):
    requested = arg.extract_plain_text().strip()
    feature = _parse_feature_name(requested)
    if not feature:
        if requested:
            await matcher.finish(f"没有找到功能：{requested}")
        await matcher.finish("用法：/关闭功能 <功能>")
    await _handle_feature_toggle(matcher, event, feature, False)


@feature_status_cmd.handle()
@group_manager_required
async def handle_feature_status(matcher: Matcher, event: Event, arg=CommandArg()):
    requested = arg.extract_plain_text().strip()
    feature = _parse_feature_name(requested)
    if not feature:
        if requested:
            await matcher.finish(f"没有找到功能：{requested}")
        await matcher.finish("用法：/功能状态 <功能>")
    await _handle_feature_status(matcher, event, feature)


@cooldown_set_cmd.handle()
@group_manager_required
async def handle_set_cooldown(matcher: Matcher, event: Event, arg=CommandArg()):
    parts = arg.extract_plain_text().strip().split()
    if len(parts) not in {2, 3}:
        await matcher.finish(
            "用法：/设置CD <功能名> <时间> [次数]，例如：设置CD 来点 3分钟 3次"
        )

    requested_name, duration_text = parts[:2]
    count = 1
    if len(parts) == 3:
        match = re.fullmatch(r"([0-9]+)次?", parts[2])
        if match is None or int(match[1]) < 1:
            await matcher.finish("次数应为正整数，例如：3次")
        count = int(match[1])
    command_name = _canonical_command_name(requested_name)
    if command_name is None:
        await matcher.finish(f"没有找到功能：{requested_name}")

    seconds = _parse_duration(duration_text)
    if seconds is None:
        await matcher.finish("时间格式错误，例如：5秒、1分钟、1小时30分钟、无限制")

    group_id = get_group_id(event)
    if group_id is None:
        await matcher.finish("请在群聊中使用该命令")

    stored_seconds = seconds if seconds > 0 else None
    await asyncio.to_thread(
        set_command_cooldown,
        group_id,
        command_name,
        stored_seconds,
        int(event.get_user_id()),
        count=count,
    )
    setting_key = (group_id, command_name)
    _COMMAND_CONFIG_CACHE[setting_key] = await asyncio.to_thread(
        get_command_config,
        group_id,
        command_name,
    )
    _COMMAND_RATE_LIMITER.reset(group_id, command_name)

    if stored_seconds is None:
        await matcher.finish(f"已取消本群「{command_name}」的CD限制")
    await matcher.finish(
        f"已将本群「{command_name}」的CD设为{_format_duration(stored_seconds)}{count}次"
    )


@sub_show_cmd.handle()
@group_manager_required
async def handle_show_subscription(matcher: Matcher, event: Event):
    group_id = get_group_id(event)
    if group_id is None:
        await matcher.finish("请在群聊中使用该命令")

    room_id = get_subscription(group_id)
    if room_id is None:
        await matcher.finish("本群尚未设置订阅")

    await matcher.finish(f"当前订阅：{await _format_name(room_id)}")


@sub_set_cmd.handle()
@group_manager_required
async def handle_set_subscription(matcher: Matcher, event: Event, arg=CommandArg()):
    group_id = get_group_id(event)
    if group_id is None:
        await matcher.finish("请在群聊中使用该命令")

    room_id = _parse_room_id(arg)
    if room_id is None:
        await matcher.finish("用法：/设置订阅 <房间号>")

    set_subscription(group_id, room_id)
    await matcher.finish(f"订阅已设置：{await _format_name(room_id)}")


@nickname_set_cmd.handle()
@group_manager_required
async def handle_set_nickname(matcher: Matcher, event: Event, arg=CommandArg()):
    group_id = get_group_id(event)
    if group_id is None:
        await matcher.finish("请在群聊中使用该命令")

    room_id = get_subscription(group_id)
    if room_id is None:
        await matcher.finish("请先设置订阅")

    nickname = arg.extract_plain_text().strip()
    if not nickname:
        await matcher.finish("用法：/设置昵称 <昵称>")
    upsert_liver(room_id=room_id, uid=None, uname=None, nickname=nickname)
    await matcher.finish(f"昵称已设置：{nickname}")


@sub_remove_cmd.handle()
@group_manager_required
async def handle_remove_subscription(matcher: Matcher, event: Event):
    group_id = get_group_id(event)
    if group_id is None:
        await matcher.finish("请在群聊中使用该命令")

    removed = remove_subscription(group_id)
    if removed:
        await matcher.finish("已删除本群订阅")
    else:
        await matcher.finish("本群没有可删除的订阅")
