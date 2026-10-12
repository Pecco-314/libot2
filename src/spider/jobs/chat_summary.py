"""群聊累计500条后的自动整理；无完整批次时不调用AI。"""

import asyncio
import logging
import os

from src.common.chat_summary_service import group_ids, stage_ready, process_batch, recover_primary_summary
from src.common.chat_summary_codex import retry_ready
from src.db import chat_summary as db

logger = logging.getLogger(__name__)


async def stage_chat_summaries():
    await asyncio.to_thread(db.init_db)
    for group_id in await asyncio.to_thread(group_ids):
        try:
            staged = await asyncio.to_thread(stage_ready, group_id)
            if staged["staged"]:
                logger.info(
                    "群聊总结入队 group=%s batches=%s", group_id, staged["staged"]
                )
        except Exception:
            logger.exception("群聊总结读取失败 group=%s", group_id)


async def collect_chat_summaries():
    await asyncio.to_thread(db.init_db)
    for bid in await asyncio.to_thread(db.ready_batch_ids):
        try:
            batch = await asyncio.to_thread(db.get_batch, bid)
            if batch["group_id"] not in await asyncio.to_thread(group_ids):
                continue
            batch = await process_batch(bid)
            logger.info("群聊总结处理完成 batch=%s status=%s", bid, batch["status"])
        except Exception as exc:
            logger.warning("群聊总结失败 batch=%s error=%s", bid, type(exc).__name__)


async def recover_chat_summaries():
    await asyncio.to_thread(db.init_db)
    candidates = await asyncio.to_thread(db.recovery_candidates)
    if os.environ.get('AI_BACKEND', 'qwen') != 'codex' or not retry_ready():
        return
    groups = set(await asyncio.to_thread(group_ids))
    for item in candidates:
        if item['group_id'] not in groups:
            continue
        try:
            changed = await recover_primary_summary(item['batch_id'])
            logger.info('群聊总结主模型重分析 batch=%s changed=%s', item['batch_id'], changed)
        except Exception as exc:
            logger.warning('群聊总结主模型仍不可用 batch=%s error=%s', item['batch_id'], type(exc).__name__)
        break  # 每轮至多重分析一个批次，优先保障新消息处理。


def register_jobs(scheduler):
    # 入队与模型请求独立运行，慢请求不阻塞高峰时段的消息累计。
    scheduler.add_job(
        stage_chat_summaries,
        "interval",
        minutes=1,
        id="chat_summary_stage",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )
    scheduler.add_job(
        collect_chat_summaries,
        "interval",
        minutes=1,
        id="chat_summary_batches",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )
    scheduler.add_job(
        recover_chat_summaries, 'interval', minutes=1, id='chat_summary_recovery',
        replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=60)
