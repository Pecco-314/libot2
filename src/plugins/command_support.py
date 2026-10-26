"""命令共用的用户消歧、权限校验、直播场次解析与日志。"""

from __future__ import annotations

import asyncio
import logging

from nonebot.adapters.onebot.v11 import Event
from nonebot.matcher import Matcher

from src.db.event import (
    get_latest_live_session,
    list_live_sessions_by_date,
    list_uids_by_uname,
)
from src.plugins.utils import is_group_manager
from src.common.time_args import normalize_date, parse_date_index

logger = logging.getLogger("libot.commands")


async def _check_modify_permission(info: dict, event: Event) -> bool:
    user_id = int(event.get_user_id())
    return user_id == info["adder_uid"] or await is_group_manager(event)


async def _resolve_user_uid(matcher: Matcher, query: str) -> int:
    """同名用户必须通过 UID 消歧，数字输入不依赖名字索引。"""
    if query.isdigit():
        return int(query)
    matches = await asyncio.to_thread(list_uids_by_uname, query)
    if not matches:
        await matcher.finish(f"未找到用户：{query}")
    if len(matches) > 1:
        choices = "、".join(f"{query}（{uid}）" for uid in matches[:10])
        suffix = "……" if len(matches) > 10 else ""
        await matcher.finish(f"名称对应多个用户，请改用UID：{choices}{suffix}")
    return matches[0]


def _parse_concurrent_args(text: str) -> tuple[str | None, int | None]:
    return parse_date_index(text)


def _resolve_live_session(
    room_id: int, date_str: str | None, session_index: int | None
) -> tuple[dict, str, bool]:
    if date_str is None:
        target_session = get_latest_live_session(room_id)
        if target_session is None:
            raise ValueError("没有找到直播记录")
        ongoing = bool(target_session.get("ongoing"))
        session_label = "当前直播" if ongoing else "最近直播"
        return target_session, session_label, ongoing

    date_str = normalize_date(date_str)

    sessions = list_live_sessions_by_date(room_id, date_str)
    if not sessions:
        raise ValueError("没有找到对应日期的直播记录")

    if session_index is None:
        target_session = sessions[-1]
        session_index_label = len(sessions)
    else:
        if session_index <= 0 or session_index > len(sessions):
            raise ValueError("场次超出范围")
        target_session = sessions[session_index - 1]
        session_index_label = session_index

    session_label = f"{date_str}第{session_index_label}场直播"
    ongoing = bool(target_session.get("ongoing"))
    return target_session, session_label, ongoing
