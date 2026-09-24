from __future__ import annotations

import asyncio
import logging

from apscheduler.triggers.cron import CronTrigger
from scripts.backfill_name_history_index import backfill
from src.db.sqlite import DEFAULT_DB_PATH

logger = logging.getLogger(__name__)


async def update_name_history() -> None:
    try:
        await asyncio.to_thread(backfill, DEFAULT_DB_PATH, 5000)
    except Exception:
        logger.exception("daily name index update failed")


def register_jobs(scheduler) -> None:
    scheduler.add_job(
        update_name_history,
        CronTrigger(hour=7, minute=0, timezone="Asia/Shanghai"),
        id="name_history_index", replace_existing=True,
        max_instances=1, coalesce=True, misfire_grace_time=3600,
    )
