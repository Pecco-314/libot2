from __future__ import annotations

from nonebot.adapters import Message
from nonebot.adapters.onebot.v11 import Event, GroupMessageEvent


def get_group_id(event: Event) -> int | None:
    if isinstance(event, GroupMessageEvent):
        return int(event.group_id)
    group_id = getattr(event, "group_id", None)
    return int(group_id) if group_id is not None else None


def parse_user_id(arg: Message) -> int | None:
    text = arg.extract_plain_text().strip()
    return int(text) if text.isdigit() else None
