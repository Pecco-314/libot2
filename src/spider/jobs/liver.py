from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.db.liver import init_liver_db, upsert_liver
from src.db.subscription import list_subscribed_room_ids
from spider.wrapper import get_master_info


logger = logging.getLogger("spider.jobs.liver")


async def collect_liver() -> None:
    room_ids = list_subscribed_room_ids()
    if not room_ids:
        logger.info("no subscribed rooms, skip liver sync")
        return

    logger.info("liver sync begin rooms=%d", len(room_ids))
    for room_id in room_ids:
        try:
            master_info = await get_master_info(room_id)
            upsert_liver(
                room_id=room_id,
                uid=master_info.uid,
                uname=master_info.uname,
                nickname=None,
            )
            logger.info(
                "liver sync room_id=%d uid=%d uname=%s",
                room_id,
                master_info.uid,
                master_info.uname,
            )
        except Exception as exc:
            logger.warning("liver sync failed room_id=%d: %s", room_id, exc)


def register_jobs(scheduler: AsyncIOScheduler) -> None:
    init_liver_db()
    scheduler.add_job(
        collect_liver,
        CronTrigger(hour=0, minute=0, second=0),
        id="liver",
        name="liver",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
    )
