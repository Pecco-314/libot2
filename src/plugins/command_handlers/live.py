"""开播列表、标签、直播记录与切片标记。"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime

from nonebot.adapters.onebot.v11 import Bot, Event, GroupMessageEvent, MessageSegment
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

from src.db.clip_marker import add_clip_marker, format_clip_marker, list_clip_markers
from src.db.event import get_latest_live_session
from src.db.live_list import (
    add_live_list,
    get_live_list,
    get_live_list_by_tag,
    get_live_list_info,
    remove_live_list,
    update_live_list_tags,
)
from src.db.liver import get_name_by_uid as _db_get_name_by_uid
from src.plugins.command_registry import (
    clip_document_cmd,
    clip_marker_cmd,
    live_list_add_cmd,
    live_list_add_tag_cmd,
    live_list_remove_cmd,
    live_list_set_tag_cmd,
    live_list_show_cmd,
    live_list_tag_members_cmd,
    live_sessions_cmd,
)
from src.plugins.command_support import (
    _check_modify_permission,
    _resolve_live_session,
    logger,
)
from src.plugins.utils import get_query_room_id, send_forward_message
from src.render.live_list import render_live_list_image
from src.render.live_sessions import render_live_sessions_image
from src.spider.api import get_master_info as _api_get_master_info
from src.spider.api import get_room_info as _api_get_room_info


@clip_marker_cmd.handle()
async def handle_clip_marker(matcher: Matcher, event: Event, arg=CommandArg()):
    if not isinstance(event, GroupMessageEvent):
        await matcher.finish("请在群聊中标记切片")
    note = " ".join(arg.extract_plain_text().split())
    if len(note) > 500:
        await matcher.finish("切片备注最多 500 字")
    room_id = get_query_room_id(event)
    timestamp = int(event.time)
    session = await asyncio.to_thread(get_latest_live_session, room_id)
    if not session or not session.get("ongoing"):
        await matcher.finish("当前没有正在进行的直播")
    start = int(session["start_ts"])
    try:
        await asyncio.to_thread(
            add_clip_marker,
            room_id,
            start,
            timestamp,
            int(event.user_id),
            int(event.group_id),
            int(event.message_id),
            note,
        )
    except ValueError as exc:
        await matcher.finish(str(exc))
    await matcher.finish("已记录切片：\n" + format_clip_marker(timestamp, start, note))


@clip_document_cmd.handle()
async def handle_clip_document(
    matcher: Matcher, bot: Bot, event: Event, arg=CommandArg()
):
    parts = arg.extract_plain_text().strip().split()
    if len(parts) > 2 or (len(parts) == 2 and not parts[1].isdigit()):
        await matcher.finish("用法：/切片文档 [YYYY-MM-DD] [直播场次]")
    date = parts[0] if parts else None
    index = int(parts[1]) if len(parts) == 2 else None
    room_id = get_query_room_id(event)
    try:
        session, label, _ = await asyncio.to_thread(
            _resolve_live_session, room_id, date, index
        )
    except ValueError as exc:
        await matcher.finish(str(exc))
    start = int(session["start_ts"])
    markers = await asyncio.to_thread(list_clip_markers, room_id, start)
    if not markers:
        await matcher.finish(f"{label}暂无切片记录")
    from zoneinfo import ZoneInfo

    started = datetime.fromtimestamp(start, ZoneInfo("Asia/Shanghai")).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    header = (
        f"切片文档 · {label}\n开播：{started}（北京时间）\n[北京时间] (开播后时间) 备注"
    )
    lines = [format_clip_marker(m["timestamp"], start, m["note"]) for m in markers]
    chunks = [header]
    for line in lines:
        if len(chunks[-1]) + len(line) + 1 > 1500:
            chunks.append(line)
        else:
            chunks[-1] += "\n" + line
    if len(chunks) == 1 and len(chunks[0]) <= 300 and len(lines) <= 5:
        await matcher.finish(MessageSegment.text(chunks[0]))
    nodes = [
        {
            "type": "node",
            "data": {
                "name": "Libot",
                "uin": bot.self_id,
                "content": MessageSegment.text(chunk),
            },
        }
        for chunk in chunks
    ]
    await send_forward_message(bot, event, nodes)


@live_list_add_cmd.handle()
async def handle_live_list_add(matcher: Matcher, event: Event, arg=CommandArg()):
    args = arg.extract_plain_text().strip().split()
    if not args:
        await matcher.finish("用法：/添加直播 <直播间号> [标签...]")

    room_id_str = args[0]
    if not room_id_str.isdigit():
        await matcher.finish("用法：/添加直播 <直播间号> [标签...]\n房间号必须为数字。")
    room_id = int(room_id_str)

    # 标签去重，保持唯一性
    tags = list(set(t.lower() for t in args[1:]))
    user_id = int(event.get_user_id())

    try:
        resp = await _api_get_room_info(room_id)
        if not resp.get("ok") or not resp.get("body", {}).get("data"):
            raise RuntimeError("网络请求失败")

        data = resp["body"]["data"]
        real_room_id = int(data["room_id"])
        uid = int(data["uid"])
    except Exception as e:
        logger.error(f"添加开播列表解析失败: {e}")
        await matcher.finish("网络请求失败，请稍后再试")

    name = _db_get_name_by_uid(uid)
    if not name:
        try:
            m_resp = await _api_get_master_info(uid)
            if m_resp.get("ok"):
                name = m_resp["body"]["data"]["info"]["uname"]
        except Exception:
            pass

    name_str = name or str(real_room_id)
    added = add_live_list(real_room_id, name_str, user_id, tags)

    if not added:
        await matcher.finish("该直播间已在列表中")

    tag_info = f" 标签: {' '.join(tags)}" if tags else ""
    msg = f"已添加到开播列表：{name_str}{tag_info}"
    if real_room_id != room_id:
        msg += f"\n(自动转为长号：{real_room_id})"

    await matcher.finish(msg)


async def _resolve_info_from_args(args: list[str]) -> dict | None:
    if not args[0].isdigit():
        return None
    room_id = int(args[0])
    info = get_live_list_info(room_id)
    if not info:
        try:
            resp = await _api_get_room_info(room_id)
            if resp.get("ok") and "data" in resp.get("body", {}):
                real_room_id = int(resp["body"]["data"]["room_id"])
                info = get_live_list_info(real_room_id)
        except Exception:
            pass
    return info


@live_list_add_tag_cmd.handle()
async def handle_live_list_add_tag(matcher: Matcher, event: Event, arg=CommandArg()):
    args = arg.extract_plain_text().strip().split()
    if len(args) < 2:
        await matcher.finish("用法：/增加标签 <直播间号> <标签...>")

    info = await _resolve_info_from_args(args)
    if not info:
        await matcher.finish("该直播间不在列表中")

    new_tags = list(set(t.lower() for t in args[1:]))
    updated_tags = list(set(info["tags"] + new_tags))
    update_live_list_tags(info["room_id"], updated_tags)
    room_name = info.get("uname") or str(info["room_id"])
    await matcher.finish(
        f"已为{room_name}直播间添加标签，当前标签：{' '.join(updated_tags) if updated_tags else '无'}"
    )


@live_list_set_tag_cmd.handle()
async def handle_live_list_set_tag(matcher: Matcher, event: Event, arg=CommandArg()):
    args = arg.extract_plain_text().strip().split()
    if len(args) < 2:
        await matcher.finish("用法：/修改标签 <直播间号> <标签...>")

    info = await _resolve_info_from_args(args)
    if not info:
        await matcher.finish("该直播间不在列表中")

    if not await _check_modify_permission(info, event):
        await matcher.finish("权限不足：仅群管理员或该直播间的添加者可修改")

    new_tags = list(set(t.lower() for t in args[1:]))
    update_live_list_tags(info["room_id"], new_tags)
    await matcher.finish(f"已修改标签，当前标签：{' '.join(new_tags)}")


@live_list_remove_cmd.handle()
async def handle_live_list_remove(matcher: Matcher, event: Event, arg=CommandArg()):
    args = arg.extract_plain_text().strip().split()
    if not args:
        await matcher.finish("用法：/删除直播 <直播间号>")

    info = await _resolve_info_from_args(args)
    if not info:
        await matcher.finish("该直播间不在列表中")

    if not await _check_modify_permission(info, event):
        await matcher.finish("权限不足：仅群管理员或该直播间的添加者可删除")

    removed = remove_live_list(info["room_id"])
    if removed:
        await matcher.finish(f"已从开播列表中删除直播间：{info['room_id']}")
    else:
        await matcher.finish("系统错误，未能删除")


@live_list_show_cmd.handle()
async def handle_live_list_show(matcher: Matcher, event: Event, arg=CommandArg()):
    args = arg.extract_plain_text().strip().split()
    filter_tag = args[0] if args else "VR+PSP+LDC"
    filter_tags = {args[0].lower()} if args else {"vr", "psp", "ldc"}
    if filter_tag == "全部":
        filter_tag = None

    rooms_info = get_live_list()
    if not rooms_info:
        await matcher.finish(
            "目前尚未添加任何直播间，请使用 /添加直播 <直播间号> 增加主播哦。"
        )

    # 默认标签取并集；显式指定时按单个标签精确匹配。
    if filter_tag:
        rooms_info = [
            r
            for r in rooms_info
            if filter_tags.intersection(t.lower() for t in r["tags"])
        ]
        if not rooms_info:
            await matcher.finish(f"当前列表中未找到包含标签 [{filter_tag}] 的直播间。")

    try:
        image_path = await render_live_list_image(rooms_info, filter_tag)
    except Exception as e:
        logger.error(f"渲染开播列表失败: {e}")
        await matcher.finish("查询或渲染失败，请稍后再试。")

    if not image_path:
        # 当数据全被 API 给过滤掉(都处于未开播状态)的情况
        msg = f"当前 {'标签 [' + filter_tag + '] 内的' if filter_tag else ''}列表未发现正在直播的主播！"
        await matcher.finish(msg)

    await matcher.finish(MessageSegment.image(file=str(image_path)))


@live_list_tag_members_cmd.handle()
async def handle_live_list_tag_members(
    matcher: Matcher, bot: Bot, event: Event, arg=CommandArg()
):
    tag = arg.extract_plain_text().strip()
    if not tag:
        await matcher.finish("用法：/查看标签 <标签>")

    rooms = get_live_list_by_tag(tag)
    if not rooms:
        await matcher.finish(f"当前列表中未找到标签 [{tag}] 的直播间。")

    # 不查直播状态：这是标签的完整收录列表，而非当前开播列表。
    lines = [
        f"{room.get('uname') or room['room_id']}（{room['room_id']}）" for room in rooms
    ]
    header = f"标签 [{tag}] 下的直播间（{len(lines)} 个）"
    chunk_size = 25
    if len(lines) <= 5:
        await matcher.finish(f"{header}\n" + "\n".join(lines))

    nodes = [
        {
            "type": "node",
            "data": {
                "name": "LiBot",
                "uin": event.self_id,
                "content": header,
            },
        }
    ]
    for start in range(0, len(lines), chunk_size):
        nodes.append(
            {
                "type": "node",
                "data": {
                    "name": "LiBot",
                    "uin": event.self_id,
                    "content": "\n".join(lines[start : start + chunk_size]),
                },
            }
        )
    try:
        await send_forward_message(bot, event, nodes)
    except Exception as exc:
        logger.error("发送标签直播间列表失败: %s", exc)
        await matcher.finish("发送列表失败，请稍后再试。")


@live_sessions_cmd.handle()
async def handle_live_sessions(matcher: Matcher, event: Event, arg=CommandArg()):
    room_id = get_query_room_id(event)

    arg_str = arg.extract_plain_text().strip()

    if not arg_str:
        month_str = datetime.now().strftime("%Y%m")
    else:
        if re.match(r"^20\d{2}-(0[1-9]|1[0-2])$", arg_str):
            month_str = arg_str.replace("-", "")
        elif re.match(r"^20\d{2}(0[1-9]|1[0-2])$", arg_str):
            month_str = arg_str
        elif re.match(r"^(0[1-9]|1[0-2])$", arg_str):
            month_str = f"{datetime.now().year}{arg_str}"
        else:
            await matcher.finish("月份格式错误，请使用如 202509 或 2025-09 的格式")

    try:
        image_path = await render_live_sessions_image(room_id, month_str)
    except Exception as e:
        logger.error(f"渲染直播记录图片失败: {e}")
        await matcher.finish("数据获取或图片渲染失败，请稍后再试。")

    if not image_path:
        await matcher.finish(f"未找到房间 {room_id} 在 {month_str} 的直播记录")

    await matcher.finish(MessageSegment.image(file=str(image_path)))
