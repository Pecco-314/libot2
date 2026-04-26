from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.db.activity import init_activity_db, insert_activity
from src.db.subscription import list_subscribed_room_ids
from src.db.liver import get_uid_by_roomid
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
            if uid is None:
                logger.info("activity sync skip room_id=%d because uid missing", room_id)
                continue

            history_items = await get_space_history(uid)
            if not history_items:
                continue

            for item in reversed(history_items):
                inserted = insert_activity(
                    activity_id=item.activity_id,
                    room_id=room_id,
                    uid=item.uid,
                    uname=item.uname,
                    timestamp=item.timestamp,
                    dy_type=item.dy_type,
                    orig_type=item.orig_type,
                    card=item.card,
                    emoji_details=item.emoji_details,
                )
                if inserted:
                    logger.info(
                        "activity inserted room_id=%d activity_id=%s uname=%s",
                        room_id,
                        item.activity_id,
                        item.uname,
                    )
        except Exception as exc:
            logger.warning("activity sync failed room_id=%d: %s", room_id, exc)


def register_jobs(scheduler: AsyncIOScheduler) -> None:
    init_activity_db()
    scheduler.add_job(
        collect_activity,
        "interval",
        seconds=10,
        id="activity",
        name="activity",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )
