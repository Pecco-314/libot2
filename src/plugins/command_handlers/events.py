"""弹幕、全文内容与醒目留言查询。"""

from __future__ import annotations

from src.common.time_args import is_date_arg, parse_date, normalize_date

import asyncio
import re
from datetime import datetime

from nonebot.adapters.onebot.v11 import Bot, Event, Message, MessageSegment
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

from src.common.danmaku_rank import parse_rank_period
from src.db.danmaku_rank import query_danmaku_rank
from src.db.event import (
    list_events_by_uid,
    list_recent_events_by_content,
    list_recent_events_by_uid,
)
from src.plugins.command_registry import (
    content_search_cmd,
    danmaku_rank_cmd,
    events_cmd,
    superchat_cmd,
)
from src.plugins.command_support import (
    _parse_concurrent_args,
    _resolve_live_session,
    _resolve_user_uid,
    logger,
)
from src.plugins.utils import (
    get_query_room_id,
    is_group_manager,
    send_forward_message,
)
from src.render.danmaku_logs import render_event_pages
from src.render.danmaku_rank import render_danmaku_rank
from src.render.superchat import get_daily_superchat_images, get_user_superchat_images
from src.spider.wrapper import get_name_by_roomid, get_name_by_uid
from src.plugins.user_query_batch import run_user_batch

_CONTENT_SEARCH_REVEAL_WORD = "reveal"


_CONTENT_SEARCH_MAX_KEYWORD_LENGTH = 50


_CONTENT_SEARCH_HIDDEN_RANGE = (
    int(datetime(2026, 3, 18).timestamp()),
    int(datetime(2026, 3, 22).timestamp()),
)


@superchat_cmd.handle()
async def handle_superchat(matcher: Matcher, bot: Bot, event: Event, arg=CommandArg()):
    room_id = get_query_room_id(event)

    query = arg.extract_plain_text().strip()
    if not query:
        images = await get_daily_superchat_images(
            room_id, datetime.now(), chunk_size=40
        )
        empty_message = "今天没有找到醒目留言"
    elif is_date_arg(query):
        try:
            day = parse_date(query)
        except ValueError as exc:
            await matcher.finish(str(exc))
        images = await get_daily_superchat_images(room_id, day, chunk_size=40)
        empty_message = f"{query} 没有找到醒目留言"
    else:
        uid = await _resolve_user_uid(matcher, query, batch=lambda uids: run_user_batch(
            bot, event, query, uids, handle_superchat))

        user_name = await get_name_by_uid(uid) or query
        images = await get_user_superchat_images(
            room_id,
            uid,
            user_name,
            chunk_size=40,
        )
        empty_message = f"没有找到 {user_name}（UID {uid}）的醒目留言"

    if not images:
        await matcher.finish(empty_message)

    nodes = []
    for img in images:
        nodes.append(
            {
                "type": "node",
                "data": {
                    "name": "Libot",
                    "uin": bot.self_id,
                    "content": MessageSegment.image(file=str(img)),
                },
            }
        )

    try:
        await send_forward_message(bot, event, nodes)
    except Exception as e:
        logger.error("发送醒目留言转发消息失败: %s", e)


@danmaku_rank_cmd.handle()
async def handle_danmaku_rank(matcher: Matcher, event: Event, arg=CommandArg()):
    room_id = get_query_room_id(event)

    raw = arg.extract_plain_text()
    try:
        period = parse_rank_period(raw)
        if period is None:
            date_str, session_index = _parse_concurrent_args(raw)
            target_session, session_label, _ongoing = await asyncio.to_thread(
                _resolve_live_session, room_id, date_str, session_index
            )
            start_ts = int(target_session["start_ts"])
            end = target_session["end_ts"]
            end_ts = int(end if end is not None else datetime.now().timestamp()) + 1
        else:
            start_ts, end_ts, session_label = period
    except ValueError as exc:
        await matcher.finish(str(exc))
    top_items = await asyncio.to_thread(query_danmaku_rank, room_id, start_ts, end_ts)
    if not top_items:
        await matcher.finish("暂无弹幕数据")

    uname = await get_name_by_roomid(room_id) or str(room_id)
    title = f"{uname} 弹幕榜（{session_label}）"
    result = await render_danmaku_rank(title, top_items)

    message = Message(
        [
            MessageSegment.text(f"{session_label}弹幕榜："),
            MessageSegment.image(file=str(result["image_path"])),
        ]
    )
    await matcher.finish(message)


def _parse_events_args(text: str) -> tuple[str | None, str | None, int | None]:
    parts = [p for p in text.strip().split() if p]
    if not parts:
        return None, None, None
    if len(parts) > 3:
        raise ValueError("用法：/查弹幕 <UID/用户名> [日期/数量] [日期/数量]")

    target = parts[0]
    date_str: str | None = None
    limit: int | None = None
    for param in parts[1:]:
        if param.isdigit():
            if limit is not None:
                raise ValueError("日期和数量各只能填写一次")
            limit = int(param)
            if limit <= 0 or limit > 2000:
                raise ValueError("查询数量必须在1到2000之间")
            continue

        try:
            param = normalize_date(param)
        except ValueError as exc:
            raise ValueError(
                "参数格式错误，请输入数量或日期（如2026-10-26、昨天）"
            ) from exc
        if date_str is not None:
            raise ValueError("日期和数量各只能填写一次")
        date_str = param

    return target, date_str, limit


@events_cmd.handle()
async def handle_events(matcher: Matcher, bot: Bot, event: Event, arg=CommandArg()):
    room_id = get_query_room_id(event)

    try:
        query_user, date_str, limit = _parse_events_args(arg.extract_plain_text())
    except ValueError as exc:
        await matcher.finish(str(exc))
    if query_user is None:
        await matcher.finish("用法：/查弹幕 <UID/用户名> [日期/数量] [日期/数量]")

    tail = arg.extract_plain_text().strip().split(maxsplit=1)
    suffix = ' ' + tail[1] if len(tail) > 1 else ''
    uid = await _resolve_user_uid(matcher, query_user, batch=lambda uids: run_user_batch(
        bot, event, query_user, uids, handle_events, lambda uid: str(uid) + suffix))

    start_ts = None
    end_ts = None
    title_suffix = ""

    if date_str is None and limit is None:
        limit = 100

    if date_str is not None:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        start_dt = datetime.combine(target_date, datetime.min.time())
        end_dt = datetime.combine(target_date, datetime.max.time())
        start_ts = int(start_dt.timestamp())
        end_ts = int(end_dt.timestamp())

    if limit is not None:
        if date_str is not None:
            title_suffix = f"截至 {date_str} 最近 {limit} 条"
        else:
            title_suffix = f"最近 {limit} 条"
    else:
        title_suffix = date_str or ""

    if limit is not None:
        events = await asyncio.to_thread(
            list_recent_events_by_uid,
            room_id,
            uid,
            limit,
            end_ts=end_ts,
        )
    else:
        assert start_ts is not None and end_ts is not None
        events = await asyncio.to_thread(list_events_by_uid, room_id, uid, start_ts, end_ts)

    if not events:
        await matcher.finish("暂无记录")

    uname = str(events[-1].get("uname") or query_user or uid)
    title = f"{uname} 的弹幕记录（{title_suffix}）"

    pages = await render_event_pages(title, events, show_date=(limit is not None))
    if not pages:
        await matcher.finish("暂无记录")

    nodes = []
    for img in pages:
        nodes.append(
            {
                "type": "node",
                "data": {
                    "name": "Libot",
                    "uin": bot.self_id,
                    "content": MessageSegment.image(file=str(img)),
                },
            }
        )

    try:
        await send_forward_message(bot, event, nodes)
    except Exception as exc:
        logger.error("发送弹幕记录失败: %s", exc)


def _parse_content_search_args(
    raw_arg: str,
) -> tuple[str, str | None, int, bool]:
    argument_text = raw_arg.strip()
    if not argument_text:
        raise ValueError("用法：/有谁说过 <关键词> [日期/数量] [日期/数量]")

    tokens = argument_text.split()
    reveal_hidden = (
        len(tokens) >= 2 and tokens[-1].lower() == _CONTENT_SEARCH_REVEAL_WORD
    )
    if reveal_hidden:
        argument_text = " ".join(tokens[:-1]).strip()

    keyword = argument_text
    date_str: str | None = None
    limit = 100
    limit_seen = False
    for _ in range(2):
        parts = keyword.rsplit(maxsplit=1)
        if len(parts) != 2:
            break
        candidate = parts[1]
        if candidate.isdigit():
            if limit_seen:
                raise ValueError("日期和数量各只能填写一次")
            parsed_limit = int(candidate)
            if parsed_limit < 1 or parsed_limit > 2000:
                raise ValueError("数量必须在1到2000之间")
            limit = parsed_limit
            limit_seen = True
            keyword = parts[0].strip()
            continue
        if is_date_arg(candidate):
            if date_str is not None:
                raise ValueError("日期和数量各只能填写一次")
            try:
                candidate = normalize_date(candidate)
            except ValueError as exc:
                raise ValueError(str(exc)) from exc
            date_str = candidate
            keyword = parts[0].strip()
            continue
        break

    quote_pairs = {('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")}
    if len(keyword) >= 2 and (keyword[0], keyword[-1]) in quote_pairs:
        keyword = keyword[1:-1]
    if not keyword:
        raise ValueError("关键词不能为空")
    if len(keyword) > _CONTENT_SEARCH_MAX_KEYWORD_LENGTH:
        raise ValueError(f"关键词不能超过{_CONTENT_SEARCH_MAX_KEYWORD_LENGTH}个字符")

    return keyword, date_str, limit, reveal_hidden


@content_search_cmd.handle()
async def handle_content_search(
    matcher: Matcher,
    bot: Bot,
    event: Event,
    arg=CommandArg(),
):
    room_id = get_query_room_id(event)

    try:
        keyword, date_str, limit, reveal_hidden = _parse_content_search_args(
            arg.extract_plain_text()
        )
    except ValueError as exc:
        await matcher.finish(str(exc))

    user_is_manager = await is_group_manager(event, bot)

    if reveal_hidden and not user_is_manager:
        await matcher.finish("没有这样的功能")

    end_ts: int | None = None
    if date_str is not None:
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        end_ts = int(datetime.combine(target_date, datetime.max.time()).timestamp())

    events = await asyncio.to_thread(
        list_recent_events_by_content,
        room_id,
        keyword,
        limit,
        None if reveal_hidden else _CONTENT_SEARCH_HIDDEN_RANGE,
        end_ts,
    )
    if not events:
        await matcher.finish(f"未找到包含“{keyword}”的记录")

    display_keyword = keyword if len(keyword) <= 30 else f"{keyword[:30]}…"
    date_prefix = f"截至 {date_str}，" if date_str is not None else ""
    title = f"{date_prefix}包含“{display_keyword}”的最近 {len(events)} 条发言"
    pages = await render_event_pages(
        title,
        events,
        page_size=100,
        show_date=True,
        show_uname=True,
        merge_events=False,
    )
    if not pages:
        await matcher.finish("暂无记录")

    nodes = [
        {
            "type": "node",
            "data": {
                "name": "Libot",
                "uin": bot.self_id,
                "content": MessageSegment.image(file=str(img)),
            },
        }
        for img in pages
    ]
    try:
        await send_forward_message(bot, event, nodes)
    except Exception as exc:
        logger.error("发送关键词搜索结果失败: %s", exc)
