from __future__ import annotations

import logging
import time
from functools import wraps

from nonebot import get_bot
from nonebot.adapters.onebot.v11 import (
    Bot,
    Event,
    GroupMessageEvent,
    Message,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.matcher import Matcher

from src.db.subscription import (
    get_subscription,
    get_subscription_feature,
    list_subscribed_group_ids,
)
from src.spider.wrapper import get_name_by_roomid

DEFAULT_QUERY_ROOM_ID = 1967216004
logger = logging.getLogger("libot.plugins.utils")


def get_group_id(event: Event) -> int | None:
    # 群临时私聊事件会额外携带来源 group_id，但发送目标仍应是私聊。
    if isinstance(event, PrivateMessageEvent):
        return None
    if isinstance(event, GroupMessageEvent):
        return int(event.group_id)
    group_id = getattr(event, "group_id", None)
    return int(group_id) if group_id is not None else None


def get_query_room_id(event: Event) -> int:
    """查询命令优先使用群订阅，私聊或未订阅群默认查询三理。"""
    group_id = get_group_id(event)
    if group_id is not None:
        room_id = get_subscription(group_id)
        if room_id is not None:
            return room_id
    return DEFAULT_QUERY_ROOM_ID


async def send_forward_message(
    bot: Bot,
    event: Event,
    messages: list[dict],
):
    """按事件来源发送群聊或私聊合并转发。"""
    if isinstance(event, GroupMessageEvent):
        return await bot.call_api(
            "send_group_forward_msg",
            group_id=int(event.group_id),
            messages=messages,
        )
    return await bot.call_api(
        "send_private_forward_msg",
        user_id=int(event.get_user_id()),
        messages=messages,
    )


def _parse_room_id(arg: Message) -> int | None:
    text = arg.extract_plain_text().strip()
    return int(text) if text.isdigit() else None

async def _format_name(room_id: int | None) -> str:
    if room_id is None:
        return "主播"
    try:
        uname = await get_name_by_roomid(room_id)
    except Exception:
        return f"房间{room_id}"

    return uname if uname else f"房间{room_id}"


async def is_group_manager(event: Event, bot: Bot | None = None) -> bool:
    """Return whether the sender is an owner/admin of the current QQ group."""
    group_id = get_group_id(event)
    if group_id is None:
        return False

    try:
        current_bot = bot or get_bot(str(event.self_id))
        member = await current_bot.call_api(
            "get_group_member_info",
            group_id=group_id,
            user_id=int(event.get_user_id()),
            no_cache=True,
        )
        return member.get("role") in {"owner", "admin"}
    except Exception as exc:
        # GroupMessageEvent normally contains the same OneBot role. Keep commands
        # usable during a transient API failure without falling back to local ACLs.
        sender_role = getattr(getattr(event, "sender", None), "role", None)
        logger.warning(
            "查询群成员权限失败，使用事件中的身份: group_id=%s user_id=%s role=%s error=%s",
            group_id,
            event.get_user_id(),
            sender_role,
            exc,
        )
    return sender_role in {"owner", "admin"}


def group_manager_required(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        matcher = next((arg for arg in args if isinstance(arg, Matcher)), None)
        event = next((arg for arg in args if isinstance(arg, Event)), None)

        if matcher is None:
            matcher = kwargs.get("matcher")
        if event is None:
            event = kwargs.get("event")

        if isinstance(matcher, Matcher) and isinstance(event, Event):
            group_id = get_group_id(event)
            if group_id is None:
                await matcher.finish("请在群聊中使用该命令")
                return

            bot = next((arg for arg in args if isinstance(arg, Bot)), None)
            if bot is None:
                bot = kwargs.get("bot")
            if not await is_group_manager(event, bot):
                await matcher.finish("权限不足：该命令仅管理员可用")
                return

        return await func(*args, **kwargs)

    return wrapper

def user_cooldown(seconds: float = 180, *, manager_exempt: bool = True):
    last_used: dict[int, float] = {}

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            matcher = next(
                (arg for arg in args if isinstance(arg, Matcher)),
                kwargs.get("matcher"),
            )
            event = next(
                (arg for arg in args if isinstance(arg, Event)),
                kwargs.get("event"),
            )

            if isinstance(matcher, Matcher) and isinstance(event, Event):
                is_exempt = (
                    manager_exempt
                    and await is_group_manager(event)
                )
                if not is_exempt:
                    user_id = int(event.get_user_id())
                    now = time.monotonic()
                    previous = last_used.get(user_id)
                    if previous is not None and now - previous < seconds:
                        await matcher.finish(
                            Message([
                                MessageSegment.at(user_id),
                                MessageSegment.text(" CD中"),
                            ])
                        )
                    last_used[user_id] = now

            return await func(*args, **kwargs)

        return wrapper

    return decorator

def subscription_dev_required(func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        matcher = next((arg for arg in args if isinstance(arg, Matcher)), None)
        event = next((arg for arg in args if isinstance(arg, Event)), None)

        if matcher is None:
            matcher = kwargs.get("matcher")
        if event is None:
            event = kwargs.get("event")

        if isinstance(matcher, Matcher) and isinstance(event, Event):
            group_id = get_group_id(event)
            if group_id is None:
                await matcher.finish("请在群聊中使用该命令")
                return None
            
            if not get_subscription_feature(group_id, "dev"):
                await matcher.finish("没有这种功能")
                return None
            return await func(*args, **kwargs)

        room_id = kwargs.get("room_id")
        if room_id is None and args:
            room_id = args[0]

        if isinstance(room_id, int):
            enabled_groups = [
                group_id
                for group_id in list_subscribed_group_ids(room_id)
                if get_subscription_feature(group_id, "dev")
            ]
            if not enabled_groups:
                return None

        return await func(*args, **kwargs)

    return wrapper
