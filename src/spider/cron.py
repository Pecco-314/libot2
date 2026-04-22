from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.common.env import load_env_file
from src.db.subscription import init_subscription_db
from src.spider.jobs import register_jobs


ROOT = Path(__file__).resolve().parents[2]
LOG_PATH = ROOT / "logs" / "spider.log"
logger = logging.getLogger("spider.cron")


def _setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s | %(message)s")

    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)


async def main() -> None:
    load_env_file()
    _setup_logging()
    init_subscription_db()

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
