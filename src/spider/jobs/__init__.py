from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.spider.jobs.liver_stats import register_jobs as register_liver_stats_jobs


def register_jobs(scheduler: AsyncIOScheduler) -> None:
    register_liver_stats_jobs(scheduler)
