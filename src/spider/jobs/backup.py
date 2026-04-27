from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.db.backup import backup_sqlite_db

logger = logging.getLogger("spider.jobs.backup")

async def run_db_backup() -> None:
    try:
        await asyncio.to_thread(backup_sqlite_db, keep_count=2)
        logger.info("database backup job completed successfully")
    except Exception as exc:
        logger.warning("db backup job error: %s", exc)

def register_jobs(scheduler: AsyncIOScheduler) -> None:
    scheduler.add_job(
        run_db_backup,
        CronTrigger(hour=5, minute=0, second=0),
        id="db_backup",
        name="db_backup",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
    )