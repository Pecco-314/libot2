from __future__ import annotations

import asyncio
import logging
import signal
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.common.utils import load_env_file, init_logger
from src.db.subscription import init_subscription_db
from src.spider.jobs import register_jobs


def configure_logging() -> logging.Logger:
    """所有 spider 命名空间和调度异常共用带时间的滚动日志。"""
    root = logging.getLogger()
    logger = logging.getLogger("spider")
    target = str(Path(__file__).resolve().parents[2] / "logs/spider.log")
    if not any(getattr(handler, "baseFilename", None) == target for handler in root.handlers):
        logger = init_logger("spider")
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            root.addHandler(handler)
    root.setLevel(logging.INFO)
    logger.setLevel(logging.INFO)
    for name in ("httpx", "httpcore", "apscheduler"):
        logging.getLogger(name).setLevel(logging.WARNING)
    return logger


async def main() -> None:
    load_env_file()
    logger = configure_logging()
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
