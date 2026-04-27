from __future__ import annotations

import time
import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from nonebot import get_bots
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot_plugin_apscheduler import scheduler

from src.common.utils import send_notification_email, ROOT
from src.render.activity import render_bilibili_card
from src.db.activity import get_max_activity_id, list_activities_after
from src.db.event import get_newest_live_event, is_streaming_event, is_duplicate_room_change
from src.db.state import get_state, set_state
from src.db.subscription import list_subscribed_group_ids

from .config import ACTIVITY_IMAGE_DIR
from .utils import _format_name

logger = logging.getLogger("libot.scheduler")
LOCK_FILE = ROOT / "data" / ".bot_offline.lock"
NAPCAT_PID = ROOT / "data" / ".pids" / "napcat.pid"


async def send_to_room(room_id: int, message: str) -> None:
    group_ids = list_subscribed_group_ids(room_id)
    if not group_ids:
        return

    bots = list(get_bots().values())
    if not bots:
        logger.warning("没有可用 bot，暂不发送 room_id=%d 的消息", room_id)
        return

    bot = bots[0]
    for group_id in group_ids:
        try:
            await bot.call_api("send_group_msg", group_id=group_id, message=message)
        except Exception as exc:
            logger.warning("发送群消息失败 room_id=%d group_id=%d: %s", room_id, group_id, exc)


async def send_activity_to_room(room_id: int, message: Message) -> None:
    group_ids = list_subscribed_group_ids(room_id)
    if not group_ids:
        return

    bots = list(get_bots().values())
    if not bots:
        logger.warning("没有可用 bot，暂不发送 activity room_id=%d 的消息", room_id)
        return

    bot = bots[0]
    for group_id in group_ids:
        try:
            await bot.call_api("send_group_msg", group_id=group_id, message=message)
        except Exception as exc:
            logger.warning("发送 activity 群消息失败 room_id=%d group_id=%d: %s", room_id, group_id, exc)


def _get_last_activity_id() -> int:
    last_activity_id = 0
    last_activity_id_str = get_state("last_activity_id")
    if last_activity_id_str is not None and last_activity_id_str.isdigit():
        last_activity_id = int(last_activity_id_str)
    return last_activity_id


async def _ensure_last_activity_id_initialized() -> None:
    if get_state("last_activity_id") is not None:
        return
    set_state("last_activity_id", str(get_max_activity_id()))


def _activity_image_path(activity: dict[str, Any]) -> Path:
    activity_id = str(activity.get("activity_id") or activity.get("id") or "activity")
    return ACTIVITY_IMAGE_DIR / f"{activity_id}.png"


async def _render_activity_image(activity: dict[str, Any]) -> Path | None:
    image_path = _activity_image_path(activity)
    image_path.parent.mkdir(parents=True, exist_ok=True)
    if image_path.exists():
        return image_path

    try:
        image = await render_bilibili_card(
            str(activity.get("card") or ""),
            int(activity.get("dy_type") or 0),
            int(activity.get("orig_type") or 0),
            int(activity.get("timestamp") or 0),
            activity.get("emoji_details") if isinstance(activity.get("emoji_details"), list) else [],
        )
        await asyncio.to_thread(image.image.save, str(image_path))
        return image_path
    except Exception as exc:
        logger.warning(
            "渲染 activity 图片失败 activity_id=%s room_id=%s: %s",
            activity.get("activity_id"),
            activity.get("room_id"),
            exc
        )
        import traceback
        traceback.print_exc()
        return None


async def _build_message(row: dict[str, Any]) -> str | None:
    name = await _format_name(row.get("room_id"))
    cmd = row.get("cmd")
    if cmd == "LIVE":
        return f"{name}开播了！"
    if cmd == "PREPARING":
        return f"{name}下播了..."
    if cmd == "ROOM_CHANGE":
        title = row.get("title")
        if isinstance(title, str) and title.strip():
            return f"{name}把直播标题修改为：{title.strip()}"
    return None


@scheduler.scheduled_job(
    "interval",
    seconds=1,
    id="libot_live_event_watcher",
    name="libot_live_event_watcher",
    max_instances=1,
    coalesce=True,
    misfire_grace_time=1,
)
async def watch_live_events() -> None:
    row = get_newest_live_event()
    if row is None:
        return
    row_id = int(row.get("id"))
    room_id = int(row.get("room_id"))
    if is_streaming_event(row) or is_duplicate_room_change(row):
        set_state("last_event_id", str(row_id))
        return
    last_event_id = 0
    last_event_id_str = get_state("last_event_id")
    if last_event_id_str is not None and last_event_id_str.isdigit():
        last_event_id = int(last_event_id_str)
    if row_id <= last_event_id:
        return
    set_state("last_event_id", str(row_id))
    message = await _build_message(row)
    if message is None:
        return
    await send_to_room(room_id, message)


@scheduler.scheduled_job(
    "interval",
    seconds=1,
    id="libot_activity_watcher",
    name="libot_activity_watcher",
    max_instances=1,
    coalesce=True,
    misfire_grace_time=1,
)
async def watch_activities() -> None:
    await _ensure_last_activity_id_initialized()

    last_activity_id = _get_last_activity_id()
    rows = list_activities_after(last_activity_id)
    if not rows:
        return

    for row in rows:
        timestamp = int(row.get("timestamp") or 0)
        upload_time = datetime.fromtimestamp(timestamp)
        activity_id = row.get("id")
        if datetime.now() - upload_time > timedelta(minutes=10):
            set_state("last_activity_id", activity_id)
            continue
        logger.info("发现新动态，开始渲染")
        image_path = await _render_activity_image(row)
        if image_path is None:
            set_state("last_activity_id", activity_id)
            continue

        uname = str(row.get("uname") or "UP主")
        message = Message([
            MessageSegment.text(f"{uname}发布了新动态！"),
            MessageSegment.image(file=str(image_path)),
        ])
        await send_activity_to_room(int(row.get("room_id") or 0), message)
        set_state("last_activity_id", activity_id)


@scheduler.scheduled_job(
    "cron",
    minute="*",
    id="libot_bot_monitor",
    name="libot_bot_monitor",
    max_instances=1,
    coalesce=True,
    misfire_grace_time=30,
)
@scheduler.scheduled_job(
    "cron",
    minute="*",  # 每 1 分钟执行一次
    id="libot_bot_monitor",
    name="libot_bot_monitor",
    max_instances=1,
    coalesce=True,
    misfire_grace_time=30,
)
async def check_bot_status() -> None:
    try:
        bots = get_bots()
        is_offline = False

        # 第一重检查：本地 WebSocket 连接是否彻底断开（Napcat 进程死亡或重启）
        if not bots:
            is_offline = True
        else:
            # 第二重检查：获取第一个 bot 实例，查验其与腾讯服务器的真实连接状态
            bot = list(bots.values())[0]
            try:
                # 调用 OneBot 标准 API 获取真实状态，无消息打扰
                status = await bot.get_status()
                if not status.get("online"):
                    is_offline = True
            except Exception as api_exc:
                # 如果 API 调用超时或报错，说明 Napcat 内部已经卡死或无响应
                logger.warning("Bot status API check failed: %s", api_exc)
                is_offline = True

        # 状态判定与邮件发送逻辑
        if is_offline:
            if NAPCAT_PID.exists():
                uptime = time.time() - NAPCAT_PID.stat().st_mtime
                if uptime < 120:
                    logger.info("Bot offline but Napcat is warming up (uptime: %.1fs), skipping alert", uptime)
                    return

            if not LOCK_FILE.exists():
                logger.warning("Bot is offline from QQ, triggering email notification")
                send_notification_email(
                    subject="[警告] 机器人掉线通知",
                    content="检测到机器人与 QQ 服务器的连接已断开。"
                )
                LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
                LOCK_FILE.touch()
            else:
                logger.debug("Bot is still offline, skipped sending email due to lock file")
        else:
            logger.debug("Bot is online")
            if LOCK_FILE.exists():
                LOCK_FILE.unlink(missing_ok=True)
                logger.info("Bot connection recovered, removing lock file")
                
    except Exception as exc:
        logger.error("failed to check bot status: %s", exc)