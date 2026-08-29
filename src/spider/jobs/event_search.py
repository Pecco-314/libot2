from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from src.db.event_search import EventSearchUnavailable, index_pending_events


logger = logging.getLogger("spider.jobs.event_search")


async def update_event_search_index() -> None:
    try:
        result = await asyncio.to_thread(
            index_pending_events,
            batch_size=5_000,
            max_batches=4,
        )
    except EventSearchUnavailable as exc:
        logger.error("event search index requires rebuild: %s", exc)
        return
    except Exception:
        logger.exception("event search index update failed")
        return

    if result["scanned_events"]:
        logger.info(
            "event search indexed=%d scanned=%d pending=%d last_id=%d",
            result["indexed_documents"],
            result["scanned_events"],
            result["pending_events"],
            result["last_indexed_event_id"],
        )


def register_jobs(scheduler: AsyncIOScheduler) -> None:
    scheduler.add_job(
        update_event_search_index,
        IntervalTrigger(seconds=30),
        id="event_search_index",
        name="event search index",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )
