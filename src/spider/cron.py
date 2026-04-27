from __future__ import annotations

import asyncio
import logging
import signal

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.common.utils import load_env_file, init_logger
from src.db.subscription import init_subscription_db
from src.spider.jobs import register_jobs


async def main() -> None:
    load_env_file()
    init_subscription_db()
    logger = init_logger("spider")

    stop_event = asyncio.Event()
    scheduler = AsyncIOScheduler()
    register_jobs(scheduler)

    loop = asyncio.get_running_loop()

    def _stop(signame: str) -> None:
        logger.info("received %s signal", signame)
        stop_event.set()

    for signame in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, signame)
        try:
            loop.add_signal_handler(sig, _stop, signame)
        except (NotImplementedError, RuntimeError, ValueError):
            signal.signal(sig, lambda _signum, _frame, name=signame: _stop(name))

    scheduler.start()
    logger.info("cron started with apscheduler jobs=%d", len(scheduler.get_jobs()))
    try:
        await stop_event.wait()
    finally:
        scheduler.shutdown(wait=False)
        logger.info("cron exited")


if __name__ == "__main__":
    asyncio.run(main())
