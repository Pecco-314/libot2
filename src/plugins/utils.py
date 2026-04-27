from __future__ import annotations

from functools import wraps

from nonebot.adapters.onebot.v11 import Event, Message, GroupMessageEvent
from nonebot.matcher import Matcher

from src.db.manager import ensure_initial_manager, is_manager
from src.db.subscription import list_subscribed_group_ids, is_subscription_dev_enabled
from src.spider.wrapper import get_name_by_roomid

from .config import INITIAL_MANAGER_QQ

def get_group_id(event: Event) -> int | None:
    if isinstance(event, GroupMessageEvent):
        return int(event.group_id)
    group_id = getattr(event, "group_id", None)
    return int(group_id) if group_id is not None else None

def parse_user_id(arg: Message) -> int | None:
    text = arg.extract_plain_text().strip()
    return int(text) if text.isdigit() else None

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

            if INITIAL_MANAGER_QQ is None:
                await matcher.finish("未配置 MANAGER_QQ，无法初始化管理员")
                return

            ensure_initial_manager(group_id, INITIAL_MANAGER_QQ)

            user_id = int(event.get_user_id())
            if not is_manager(group_id, user_id):
                await matcher.finish("权限不足：该命令仅管理员可用")
                return

        return await func(*args, **kwargs)

    return wrapper

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
            if not is_subscription_dev_enabled(group_id):
                await matcher.finish("此功能测试中")
                return None
            return await func(*args, **kwargs)

        room_id = kwargs.get("room_id")
        if room_id is None and args:
            room_id = args[0]

        if isinstance(room_id, int):
            enabled_groups = [
                group_id
                for group_id in list_subscribed_group_ids(room_id)
                if is_subscription_dev_enabled(group_id)
            ]
            if not enabled_groups:
                return None

        return await func(*args, **kwargs)

    return wrapper