"""投递已入库的用户动态，不在 bot 事件循环中抓取外部数据。"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from nonebot import get_bots
from nonebot.adapters.onebot.v11 import MessageSegment
from nonebot_plugin_apscheduler import scheduler

from src.common.event_text import format_event_text
from src.common.push_features import room_activity_feature, is_push_enabled
from src.db.user_activity import finish_delivery, list_pending_deliveries, skip_delivery

logger = logging.getLogger(__name__)


def format_user_activity(event: dict) -> str:
    when = datetime.fromtimestamp(event["timestamp_ms"] / 1000, ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M:%S")
    prefix = f"{event['uname']}在{event['room_name']}的直播间"
    text, _, _ = format_event_text(event)
    if event["cmd"] == "DANMU_MSG":
        text = f"发送了弹幕：{text}"
    elif event["cmd"] == "SUPER_CHAT_MESSAGE":
        text = f"发送了{text}"
    return f"{prefix}{text}\n{when} · https://live.bilibili.com/{event['room_id']}"


@scheduler.scheduled_job("interval", seconds=2, id="libot_user_activity_delivery",
                         max_instances=1, coalesce=True, misfire_grace_time=2)
async def deliver_user_activity() -> None:
    bots = list(get_bots().values())
    if not bots:
        return
    rows = await asyncio.to_thread(list_pending_deliveries, int(time.time()))
    for row in rows:
        try:
            feature = room_activity_feature(row["event"]["room_id"], row["subscription_room"])
            if not await asyncio.to_thread(is_push_enabled, row["group_id"], feature):
                await asyncio.to_thread(skip_delivery, row["id"], int(time.time()))
                continue
            await bots[0].send_group_msg(group_id=row["group_id"],
                                         message=MessageSegment.text(format_user_activity(row["event"])))
        except Exception as exc:
            logger.warning("用户动态发送失败 delivery=%s: %s", row["id"], exc)
            await asyncio.to_thread(finish_delivery, row["id"], int(time.time()), str(exc))
        else:
            await asyncio.to_thread(finish_delivery, row["id"], int(time.time()))
