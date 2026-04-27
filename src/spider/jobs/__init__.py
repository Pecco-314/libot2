from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.spider.jobs.activity import register_jobs as register_activity_jobs
from src.spider.jobs.liver import register_jobs as register_liver_jobs
from src.spider.jobs.stats import register_jobs as register_stats_jobs
from src.spider.jobs.backup import register_jobs as register_backup_jobs


def register_jobs(scheduler: AsyncIOScheduler) -> None:
    register_activity_jobs(scheduler)
    register_liver_jobs(scheduler)
    register_stats_jobs(scheduler)
    register_backup_jobs(scheduler)
