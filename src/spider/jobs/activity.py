from __future__ import annotations

import logging
import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.common.activity_assets import ActivityAssetLocalizer
from src.db.activity import activity_exists, init_activity_db, insert_activity, get_latest_activity_timestamp
from src.db.subscription import list_subscribed_room_ids
from src.db.liver import get_uid_by_roomid
from src.spider.wrapper import get_space_history

logger = logging.getLogger("spider.jobs.activity")


async def collect_activity() -> None:
    room_ids = list_subscribed_room_ids()
    if not room_ids:
        logger.info("no subscribed rooms, skip activity sync")
        return

    rooms = ", ".join(str(rid) for rid in room_ids)
    logger.debug("activity sync begin rooms=%s", rooms)
    for room_id in room_ids:
        try:
            uid = get_uid_by_roomid(room_id)
            if uid is None:
                logger.info(
                    "activity sync skip room_id=%d because uid missing", room_id
                )
                continue

            since = await asyncio.to_thread(get_latest_activity_timestamp, room_id)
            history_items = await get_space_history(uid, since=since)
            if not history_items:
                continue

            async with ActivityAssetLocalizer() as localizer:
                for item in reversed(history_items):
                    if await asyncio.to_thread(activity_exists, item.activity_id):
                        continue
                    localized_item, assets, fully_localized = await localizer.localize(
                        item.item
                    )
                    inserted = await asyncio.to_thread(
                        insert_activity,
                        activity_id=item.activity_id,
                        room_id=room_id,
                        uid=item.uid,
                        uname=item.uname,
                        timestamp=item.timestamp,
                        dy_type_str=item.dy_type,
                        item_dict=localized_item,
                        item_remote_dict=item.item,
                        assets=assets,
                        assets_localized=fully_localized,
                    )
                    if inserted:
                        logger.info(
                            "activity inserted room_id=%d activity_id=%s "
                            "uname=%s localized=%s assets=%d",
                            room_id,
                            item.activity_id,
                            item.uname,
                            fully_localized,
                            len(assets),
                        )
        except Exception as exc:
            logger.warning("activity sync failed room_id=%d: %s", room_id, exc, exc_info=True)


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
