"""用户曾用名与历史牌子查询。"""

from __future__ import annotations

import asyncio

from nonebot.adapters.onebot.v11 import Bot, Event, MessageSegment
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

from src.db.event import list_name_history_by_name_or_uid
from src.db.fan_club import list_latest_member_medals
from src.plugins.command_registry import medal_wall_cmd, name_history_cmd
from src.plugins.command_support import _resolve_user_uid, logger
from src.plugins.utils import send_forward_message
from src.render.fan_club import render_member_medals
from src.spider.medal_wall import MedalWallError, get_public_medal_wall
from src.spider.wrapper import get_name_by_uid


@name_history_cmd.handle()
async def handle_name_history(
    matcher: Matcher, bot: Bot, event: Event, arg=CommandArg()
):
    query = arg.extract_plain_text().strip()
    if not query:
        await matcher.finish("用法：/曾用名 <UID/用户名>")
    history = await asyncio.to_thread(list_name_history_by_name_or_uid, query)
    if not history:
        await matcher.finish("没有找到符合条件的用户")
    result = f"找到{len(history)}个用户：\n"
    for i, entry in enumerate(history, start=1):
        names = entry["history"]
        try:
            current_name = await get_name_by_uid(entry["uid"])
        except Exception:
            logger.warning(f"查询用户 {entry['uid']} 的当前名称失败")
            current_name = names[-1]
        result += f"{i}. {current_name} ({', '.join(names)})\n"
    if len(result) <= 300 and len(history) <= 5:
        await matcher.finish(MessageSegment.text(result))

    # 保留完整结果及原有顺序，并限制单个转发节点的文字长度。
    chunks = []
    chunk = ""
    for line in result.splitlines(keepends=True):
        if chunk and len(chunk) + len(line) > 1500:
            chunks.append(chunk)
            chunk = ""
        while len(line) > 1500:
            chunks.append(line[:1500])
            line = line[1500:]
        chunk += line
    if chunk:
        chunks.append(chunk)
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
    try:
        await send_forward_message(bot, event, nodes)
    except Exception:
        logger.exception("发送曾用名合并转发失败")
        await matcher.finish("曾用名结果发送失败，请稍后再试")


@medal_wall_cmd.handle()
async def handle_medal_wall(
    matcher: Matcher,
    bot: Bot,
    event: Event,
    arg=CommandArg(),
):
    query = arg.extract_plain_text().strip()
    if not query:
        await matcher.finish("用法：/查牌子 <UID/用户名>")

    fallback_name = query
    member_uid = await _resolve_user_uid(matcher, query)
    matches = await asyncio.to_thread(list_name_history_by_name_or_uid, str(member_uid))
    if matches:
        names = [
            str(name) for name in matches[0].get("history") or [] if str(name).strip()
        ]
        if names:
            fallback_name = str(names[-1])

    database_medals = await asyncio.to_thread(
        list_latest_member_medals,
        member_uid,
    )
    try:
        wall = await get_public_medal_wall(member_uid)
    except MedalWallError as exc:
        logger.warning("查询公开勋章墙失败 uid=%d error=%s", member_uid, exc)
        await matcher.finish("查询公开勋章墙失败，请稍后再试")

    combined = {
        int(row["target_uid"]): {
            "target_uid": int(row["target_uid"]),
            "target_name": str(row["target_name"]),
            "level": int(row["level"]),
            "guard_level": int(row.get("guard_level") or 0),
        }
        for row in database_medals
    }
    for row in wall["medals"]:
        target_uid = int(row["target_uid"])
        existing = combined.get(target_uid)
        combined[target_uid] = {
            "target_uid": target_uid,
            "target_name": (
                str(existing["target_name"])
                if existing is not None
                else str(row["target_name"])
            ),
            "level": int(row["level"]),
            "guard_level": int(row.get("guard_level") or 0),
        }

    medals = sorted(
        combined.values(),
        key=lambda row: (
            -int(row["level"]),
            str(row["target_name"]).casefold(),
            int(row["target_uid"]),
        ),
    )
    hidden_notice = "（用户已隐藏，牌子列表不完整）" if wall["hidden"] else ""
    if not medals:
        await matcher.finish(
            f"没有找到 {wall['uname'] or fallback_name} 的可见牌子{hidden_notice}"
        )

    member_name = str(wall["uname"] or fallback_name)
    try:
        images = await asyncio.to_thread(
            render_member_medals,
            member_uid,
            member_name,
            medals,
            bool(wall["hidden"]),
        )
    except Exception as exc:
        logger.exception("渲染牌子列表失败 uid=%d", member_uid)
        await matcher.finish(f"牌子列表渲染失败：{exc}")

    nodes = [
        {
            "type": "node",
            "data": {
                "name": "Libot",
                "uin": bot.self_id,
                "content": MessageSegment.image(file=str(image_path)),
            },
        }
        for image_path in images
    ]
    try:
        await send_forward_message(bot, event, nodes)
    except Exception as exc:
        logger.error("发送牌子列表合并转发失败: %s", exc)
        await matcher.finish("牌子列表发送失败，请稍后再试")
