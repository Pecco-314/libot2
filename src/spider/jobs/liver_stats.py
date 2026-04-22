from __future__ import annotations

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.db.liver_stats import init_liver_stats_db, insert_liver_stats
from src.db.subscription import list_subscribed_room_ids
from src.spider.wrapper import get_liver_stats


logger = logging.getLogger("spider.jobs.liver_stats")


async def collect_liver_stats() -> None:
    room_ids = list_subscribed_room_ids()
    if not room_ids:
        logger.info("no subscribed rooms, skip liver stats collection")
        return

    logger.info("liver stats begin rooms=%d", len(room_ids))
    for room_id in room_ids:
        try:
            stats = await get_liver_stats(room_id)
            insert_liver_stats(
                room_id=int(stats["room_id"]),
                uid=int(stats["uid"]),
                uname=str(stats["uname"]),
                fans_num=int(stats["fans_num"]),
                guard_num=int(stats["guard_num"]),
                fan_club_num=int(stats["fan_club_num"]),
                created_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            )
            logger.info(
                "liver stats room_id=%d uname=%s fans=%d guard=%d fan_club=%d",
                int(stats["room_id"]),
                stats["uname"],
                int(stats["fans_num"]),
                int(stats["guard_num"]),
                int(stats["fan_club_num"]),
            )
        except Exception as exc:
            logger.warning("liver stats failed room_id=%d: %s", room_id, exc)


def register_jobs(scheduler: AsyncIOScheduler) -> None:
    init_liver_stats_db()
    scheduler.add_job(
        collect_liver_stats,
        CronTrigger(minute="0,30", second=0),
        id="liver_stats",
        name="liver_stats",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
    )
