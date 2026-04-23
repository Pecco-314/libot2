from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.db.activity import init_activity_db, insert_activity
from src.db.subscription import list_subscribed_room_ids
from src.db.liver import get_uid_by_roomid, get_name_by_roomid
from src.spider.wrapper import get_space_history


logger = logging.getLogger("spider.jobs.activity")


async def collect_activity() -> None:
    room_ids = list_subscribed_room_ids()
    if not room_ids:
        logger.info("no subscribed rooms, skip activity sync")
        return

    logger.info("activity sync begin rooms=%d", len(room_ids))
    for room_id in room_ids:
        try:
            uid = get_uid_by_roomid(room_id)
            uname = get_name_by_roomid(room_id)
            if uid <= 0:
                logger.info("activity sync skip room_id=%d because uid missing", room_id)
                continue

            history_items = await get_space_history(uid)
            if not history_items:
                continue

            for item in reversed(history_items):
                inserted = insert_activity(
                    activity_id=str(item.get("activity_id") or ""),
                    room_id=room_id,
                    uid=int(item.get("uid") or uid),
                    uname=str(item.get("uname") or uname),
                    timestamp=int(item.get("timestamp") or 0),
                    dy_type=int(item.get("dy_type") or 0),
                    orig_type=int(item.get("orig_type") or 0),
                    card_json_str=str(item.get("card_json_str") or ""),
                    emoji_details=item.get("emoji_details") if isinstance(item.get("emoji_details"), list) else [],
                )
                if inserted:
                    logger.info(
                        "activity inserted room_id=%d activity_id=%s uname=%s",
                        room_id,
                        item.get("activity_id"),
                        item.get("uname") or uname,
                    )
        except Exception as exc:
            logger.warning("activity sync failed room_id=%d: %s", room_id, exc)


def register_jobs(scheduler: AsyncIOScheduler) -> None:
    init_activity_db()
    scheduler.add_job(
        collect_activity,
        "interval",
        seconds=20,
        id="activity",
        name="activity",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )
