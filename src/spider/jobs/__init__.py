from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.spider.jobs.activity import register_jobs as register_activity_jobs
from src.spider.jobs.liver import register_jobs as register_liver_jobs
from src.spider.jobs.stats import register_jobs as register_stats_jobs
from src.spider.jobs.backup import register_jobs as register_backup_jobs
from src.spider.jobs.cookie_monitor import register_jobs as register_cookie_monitor_jobs
from src.spider.jobs.song_list import register_jobs as register_song_list_jobs
from src.spider.jobs.lyrics import register_jobs as register_lyrics_jobs


def register_jobs(scheduler: AsyncIOScheduler) -> None:
    register_activity_jobs(scheduler)
    register_liver_jobs(scheduler)
    register_stats_jobs(scheduler)
    register_backup_jobs(scheduler)
    register_cookie_monitor_jobs(scheduler)
    register_song_list_jobs(scheduler)
    register_lyrics_jobs(scheduler)
