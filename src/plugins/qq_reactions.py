"""群表情回应的实时采集与群内排行榜。"""
import asyncio
import logging
import time

from nonebot import on_message, on_notice
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, NoticeEvent, MessageSegment
from nonebot_plugin_apscheduler import scheduler

from src.common.qq_reactions import parse_period, top_emojis
from src.db import qq_reactions as db
from .utils import send_forward_message

logger = logging.getLogger(__name__)
db.init_db()
messages = on_message(priority=2, block=False)
notices = on_notice(priority=2, block=False)
_lookups = asyncio.Semaphore(2)
_pending = set()
_misses = {}
_members = {}


@messages.handle()
async def remember_author(event: GroupMessageEvent):
    await asyncio.to_thread(db.store_author, int(event.self_id), event.group_id,
                           event.message_id, event.user_id, int(event.time),
                           event.sender.card or event.sender.nickname or "")


async def resolve_author(bot, key):
    try:
        async with _lookups:
            message = await asyncio.wait_for(bot.get_msg(message_id=key[2]), timeout=5)
        # 缺少群号时无法验证归属，保留未知作者。
        if int(message.get("group_id") or 0) != key[1] or int(message.get("message_id") or 0) != key[2]:
            return
        sender = message.get("sender") or {}
        uid = int(sender.get("user_id") or message.get("user_id") or 0)
        if uid:
            await asyncio.to_thread(db.store_author, *key, uid, int(message.get("time") or 0),
                                   sender.get("card") or sender.get("nickname") or "")
    except Exception as exc:
        logger.debug("表情回应原消息作者暂不可用 group=%s message=%s error=%s", key[1], key[2], type(exc).__name__)
    finally:
        _pending.discard(key)
        _misses[key] = time.monotonic()
        if len(_misses) > 2000:
            _misses.pop(next(iter(_misses)))


@notices.handle()
async def remember_reaction(bot: Bot, event: NoticeEvent):
    if event.notice_type != "group_msg_emoji_like":
        return
    data = event.model_dump() if hasattr(event, "model_dump") else event.dict()
    await asyncio.to_thread(db.store_notice, data)
    key = (int(event.self_id), int(data["group_id"]), int(data["message_id"]))
    if await asyncio.to_thread(db.get_author, *key):
        return
    if key in _pending or time.monotonic() - _misses.get(key, -1000) < 600 or len(_pending) >= 20:
        return
    _pending.add(key)
    # handler 保持非阻塞调度；全局限制未知作者的并发和等待数量。
    await resolve_author(bot, key)


@scheduler.scheduled_job("cron", hour=7, minute=45, id="qq_reaction_author_cleanup", max_instances=1)
async def cleanup():
    await asyncio.to_thread(db.cleanup_authors, int(time.time()) - 35 * 86400)


async def show_rank(bot, event, matcher, text, received):
    if not isinstance(event, GroupMessageEvent):
        await matcher.finish("请在要查询的群聊中使用")
    try:
        start, end, label = parse_period(text)
    except ValueError as exc:
        await matcher.finish(str(exc))
    result = await asyncio.to_thread(db.ranking, event.group_id, start, end, received)
    if not result["items"]:
        detail = f"（有{result['unknown']}条回应无法确认原消息作者）" if received and result["unknown"] else ""
        await matcher.finish(f"这段时间没有已记录的有效{'被贴' if received else '贴'}表情{detail}")
    # 每群短期复用一次成员列表；查询失败仍可用记录中的名字与 QQ 号。
    cache_key = (bot.self_id, event.group_id)
    cached_at, names = _members.get(cache_key, (0, {}))
    if time.monotonic() - cached_at > 600:
        try:
            members = await asyncio.wait_for(bot.get_group_member_list(group_id=event.group_id), 5)
            names = {int(m["user_id"]): m.get("card") or m.get("nickname") or str(m["user_id"]) for m in members}
        except Exception:
            pass
        _members[cache_key] = (time.monotonic(), names)
    for item in result["items"]:
        item["name"] = names.get(item["uid"], item["name"])
    from src.common.qq_emoji_assets import ensure_assets
    try:
        await asyncio.wait_for(ensure_assets(eid for item in result["items"] for eid, _ in top_emojis(item["emoji"])), 15)
    except asyncio.TimeoutError:
        logger.info("表情资源预取超时，未缓存项使用名称显示")
    from src.render.qq_reactions import render_rank
    paths = await asyncio.to_thread(render_rank, result, received, label)
    if len(paths) == 1:
        await matcher.finish(MessageSegment.image(file=str(paths[0])))
    nodes = [{"type": "node", "data": {"name": "Libot", "uin": bot.self_id,
              "content": MessageSegment.image(file=str(path))}} for path in paths]
    await send_forward_message(bot, event, nodes)
