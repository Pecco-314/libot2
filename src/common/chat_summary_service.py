"""500条自动整理；按需查询及时刷新，相邻小批次合并为连续讨论。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from datetime import datetime

from src.common.chat_summary import (
    TZ,
    _complete,
    prepare_messages,
    summarize,
    validate_result,
    attach_participants,
    summary_for_model,
    correct_generation_labels,
)
from src.db import chat_context
from src.db import chat_summary as db
from src.common.chat_summary_commands import command_context
from src.db.command_config import enabled_command_groups


def group_ids():
    return enabled_command_groups("在聊什么")


def parse_summary_count(argument=""):
    text = argument.strip()
    if not text:
        return 500
    if not re.fullmatch(r"[0-9]{3,4}(?:条)?", text):
        raise ValueError("用法：在聊什么 [500—5000条]，例如：在聊什么 2000")
    count = int(text.removesuffix("条"))
    if not 500 <= count <= 5000:
        raise ValueError("消息条数范围为500—5000，按完整缓存批次取整")
    return count


async def cached_count_summary(group_id, count=500, *, db_path=db.DB_PATH, cache_path=None, bot_qq=None):
    """组合完整批次的已存结论；不调用模型，不移动累计游标。"""
    parse_summary_count(str(count))
    batches = await asyncio.to_thread(db.recent_count_batches, group_id, count, db_path)
    if not batches:
        raise ValueError("尚无总结缓存，有新增消息即可按需整理，500条自动整理")
    parts = []
    for batch in batches:
        part = json.loads(batch['result'])
        await hydrate_participants(group_id, part, batch['id'], db_path=db_path,
                                   cache_path=cache_path, bot_qq=bot_qq)
        parts.append(correct_generation_labels(part))
    first = datetime.fromtimestamp(batches[0]['start_ts'], TZ)
    last = datetime.fromtimestamp(batches[-1]['end_ts'], TZ)
    end_format = '%H:%M' if first.date() == last.date() else '%m-%d %H:%M'
    topics = []
    for batch, part in zip(batches, parts):
        start_time = datetime.fromtimestamp(batch['start_ts'], TZ)
        end_time = datetime.fromtimestamp(batch['end_ts'], TZ)
        label = f"{start_time:%m-%d %H:%M}—{end_time:%H:%M}"
        for topic in part.get('topics', []):
            topics.append({**topic, **({'batch_period': label} if len(parts) > 1 else {})})
    return {
        'period': f"{first:%m-%d %H:%M}—{last.strftime(end_format)}",
        'message_count': sum(p.get('message_count', 0) for p in parts),
        'image_message_count': sum(p.get('image_message_count', 0) for p in parts),
        'raw_message_count': sum(b['message_count'] for b in batches),
        'requested_message_count': count,
        'batch_ids': [b['id'] for b in batches],
        'topics': topics,
        'terms_to_check': [],
        'cache_hit': True,
    }


async def count_summary(group_id, count=500, *, on_generate=None, **kwargs):
    parse_summary_count(str(count))
    latest = await latest_summary(group_id, on_generate=on_generate, **kwargs)
    result = await cached_count_summary(group_id, count, **kwargs)
    for field in ('pending_message_count', 'refresh_in_progress'):
        result[field] = latest.get(field, 0)
    return result


def counted_rows(rows, bot_qq=""):
    """累计未撤回的人类消息；媒体参与计数，但未提供内容的媒体不被解读。"""
    result = []
    for row in rows:
        if str(row["user_id"]) == str(bot_qq):
            continue
        text = "".join(
            str(s.get("text", ""))
            for s in json.loads(row["segments"] or "[]")
            if s["type"] == "text"
        ).strip()
        if not text.startswith(("在聊什么", "/在聊什么")):
            result.append(row)
    return result


def stage_ready(
    group_id, *, db_path=db.DB_PATH, cache_path=None, now=None, bot_qq=None
):
    now = int(time.time()) if now is None else now
    cycle = db.get_cycle(group_id, db_path)
    rows = unconsumed_rows(
        group_id, cycle, cache_path=cache_path, now=now, bot_qq=bot_qq
    )
    db.record_term_mentions(group_id, rows, db_path)
    if cycle["claim_token"] or db.active_batch(group_id, db_path):
        return {"staged": [], "tail_count": len(rows)}
    assigned = db.assigned_ids(group_id, now - db.RAW_TTL, db_path)
    remaining = [r for r in rows if r["message_id"] not in assigned]
    batch_ids = []
    if len(remaining) >= db.BATCH_SIZE:
        batch = remaining[: db.BATCH_SIZE]
        chat_context.observe_names(group_id, batch, db_path)
        bid = db.stage_batch(group_id, batch, db_path)
        if bid is not None:
            batch_ids.append(bid)
    return {
        "staged": batch_ids,
        "tail_count": len(remaining) - (db.BATCH_SIZE if batch_ids else 0),
    }


def unconsumed_rows(group_id, cycle, *, cache_path=None, now=None, bot_qq=None):
    now = int(time.time()) if now is None else now
    rows = chat_context.list_messages(group_id, now - db.RAW_TTL, now + 1, cache_path)
    rows = counted_rows(
        rows, bot_qq if bot_qq is not None else os.environ.get("BOT_QQ", "")
    )
    cursor = (cycle["cursor_ts"], cycle["cursor_mid"])
    return [r for r in rows if (r["sent_at"], r["message_id"]) > cursor]


async def analyze_rows(
    group_id, rows, *, db_path=db.DB_PATH, bot_qq=None, previous=None, primary_only=False
):
    await asyncio.to_thread(chat_context.observe_names, group_id, rows, db_path)
    rev = await asyncio.to_thread(db.revision, db_path)
    context = await asyncio.to_thread(db.load_context, db_path, group_id)
    people = await asyncio.to_thread(chat_context.list_people, group_id, db_path)
    messages, names = prepare_messages(
        rows, people, bot_qq if bot_qq is not None else os.environ.get("BOT_QQ", "")
    )
    label = f"{datetime.fromtimestamp(rows[0]['sent_at'], TZ):%m-%d %H:%M}—{datetime.fromtimestamp(rows[-1]['sent_at'], TZ):%H:%M}"
    if messages:
        result = await summarize(
            messages,
            names,
            label,
            review=False,
            context=context,
            previous_summary=previous,
            **({'primary_only': True} if primary_only else {}),
        )
    else:
        result = {
            "topics": [],
            "terms_to_check": [],
            "period": label,
            "message_count": 0,
            "usage": [],
        }
    result.update(raw_message_count=len(rows), context_revision=rev)
    attach_participants(result, rows, people, bot_qq or os.environ.get("BOT_QQ", ""))
    return result, rev


async def hydrate_participants(
    group_id, value, batch_id, *, db_path=db.DB_PATH, cache_path=None, bot_qq=None
):
    if (
        value.get("participants_version") == 1
        and all("participants" in topic for topic in value.get("topics", []))
    ) or not batch_id:
        return value
    batch = await asyncio.to_thread(db.get_batch, batch_id, db_path)
    if not batch or json.loads(batch["result"]) != value:
        return value
    rows = await asyncio.to_thread(
        chat_context.list_messages,
        group_id,
        batch["start_ts"],
        batch["end_ts"] + 1,
        cache_path,
    )
    if rows:
        people = await asyncio.to_thread(chat_context.list_people, group_id, db_path)
        attach_participants(value, rows, people, bot_qq or os.environ.get("BOT_QQ", ""))
        await asyncio.to_thread(
            db.save_participant_metadata, batch_id, batch["result"], value, db_path
        )
    return value


def query_refresh_due(state, pending_count, *, now=None):
    """少量消息在两分钟内合并；首次查询或累计30条时立即整理。"""
    now = int(time.time()) if now is None else now
    return bool(pending_count) and (
        not state["result"]
        or pending_count >= db.QUERY_THRESHOLD
        or now - state["updated_at"] >= db.QUERY_COALESCE_SECONDS
    )


async def coalesce_query_rows(group_id, state, pending, *, db_path=db.DB_PATH,
                              cache_path=None):
    """相邻小批次以原文一起归纳，缓存按消息身份排除旧版重叠。"""
    batch_id = state["latest_batch_id"]
    if not batch_id or len(pending) >= db.BATCH_SIZE:
        return pending, None
    batch = await asyncio.to_thread(db.get_batch, batch_id, db_path)
    if (
        not batch or batch["group_id"] != group_id or batch["status"] != "complete"
        or batch["origin"] != "query"
        or batch["start_ts"] < int(time.time()) - db.RAW_TTL
        or len(pending) + batch["message_count"] > db.BATCH_SIZE
        or pending[0]["sent_at"] - batch["end_ts"] > db.QUERY_MERGE_GAP_SECONDS
    ):
        return pending, None
    keys = await asyncio.to_thread(db.batch_message_keys, batch_id, db_path)
    previous_rows = await asyncio.to_thread(
        chat_context.list_messages, group_id, batch["start_ts"], batch["end_ts"] + 1,
        cache_path,
    )
    previous_rows = [r for r in previous_rows if (r["sent_at"], r["message_id"]) in keys]
    if not keys or {(r["sent_at"], r["message_id"]) for r in previous_rows} != keys:
        return pending, None
    combined = {(r["sent_at"], r["message_id"]): r for r in [*previous_rows, *pending]}
    # 一个批次内的消息ID唯一；跨天复用ID时保持独立批次。
    if len({r["message_id"] for r in combined.values()}) != len(combined):
        return pending, None
    return [combined[key] for key in sorted(combined)], batch_id


async def latest_summary(group_id, *, db_path=db.DB_PATH, cache_path=None, bot_qq=None,
                         on_generate=None):
    await asyncio.to_thread(db.init_db, db_path)
    cycle = await asyncio.to_thread(db.get_cycle, group_id, db_path)
    remaining = await asyncio.to_thread(
        unconsumed_rows, group_id, cycle, cache_path=cache_path, bot_qq=bot_qq
    )

    async def reuse(state, pending, *, busy=False):
        if not state["result"]:
            detail = (
                "正在后台整理，请稍后查询"
                if busy
                else f"尚无总结缓存，已累计{len(pending)}条；有新增消息即可按需整理，500条自动整理"
            )
            raise ValueError(detail)
        value = json.loads(state["result"])
        await hydrate_participants(
            group_id,
            value,
            state["latest_batch_id"],
            db_path=db_path,
            cache_path=cache_path,
            bot_qq=bot_qq,
        )
        value.update(
            cache_hit=True,
            pending_message_count=len(pending),
            refresh_in_progress=busy,
            context_stale=state["context_revision"]
            != await asyncio.to_thread(db.revision, db_path),
        )
        return value

    if not query_refresh_due(cycle, len(remaining)):
        return await reuse(cycle, remaining, busy=bool(cycle["claim_token"]))
    claimed = await asyncio.to_thread(db.claim_cycle, group_id, db_path)
    if claimed is None:
        return await reuse(
            await asyncio.to_thread(db.get_cycle, group_id, db_path),
            remaining,
            busy=True,
        )
    token = claimed["claim_token"]
    try:
        remaining = await asyncio.to_thread(
            unconsumed_rows, group_id, claimed, cache_path=cache_path, bot_qq=bot_qq
        )
        if not query_refresh_due(claimed, len(remaining)):
            return await reuse(claimed, remaining)
        previous = json.loads(claimed["result"]) if claimed["result"] else None
        analysis_rows, merged_batch_id = await coalesce_query_rows(
            group_id, claimed, remaining, db_path=db_path, cache_path=cache_path
        )
        if on_generate is not None:
            await on_generate()
        value, rev = await analyze_rows(
            group_id, analysis_rows, db_path=db_path, bot_qq=bot_qq, previous=previous
        )
        value["new_message_count"] = len(remaining)
        if merged_batch_id is not None:
            value["coalesced_from_batch_id"] = merged_batch_id
        bid = await asyncio.to_thread(
            db.finish_query, group_id, analysis_rows, value, rev, token, db_path
        )
        if not bid:
            raise RuntimeError("本轮整理已被更新的任务接管，请稍后再查")
        current = await asyncio.to_thread(db.get_cycle, group_id, db_path)
        pending = await asyncio.to_thread(
            unconsumed_rows, group_id, current, cache_path=cache_path, bot_qq=bot_qq
        )
        value.update(
            cache_hit=False,
            pending_message_count=len(pending),
            refresh_in_progress=False,
            context_stale=False,
        )
        return value
    finally:
        await asyncio.to_thread(db.release_cycle, group_id, token, db_path)


async def reanalyze_batch(
    batch_id, *, db_path=db.DB_PATH, cache_path=None, bot_qq=None
):
    await asyncio.to_thread(db.init_db, db_path)
    batch = await asyncio.to_thread(db.get_batch, batch_id, db_path)
    if not batch or batch["status"] != "complete":
        raise ValueError("只能重分析已保存的批次")
    with db.connection(db_path) as conn:
        keys = {
            (r[0], r[1])
            for r in conn.execute(
                "SELECT message_id,sent_at FROM chat_summary_member WHERE batch_id=?",
                (batch_id,),
            )
        }
    rows = await asyncio.to_thread(
        chat_context.list_messages,
        batch["group_id"],
        batch["start_ts"],
        batch["end_ts"] + 1,
        cache_path,
    )
    recalled = await asyncio.to_thread(
        chat_context.recalled_ids, batch["group_id"], cache_path
    )
    expected = {(mid, ts) for mid, ts in keys if mid not in recalled}
    rows = [r for r in rows if (r["message_id"], r["sent_at"]) in expected]
    if not rows or len(rows) != len(expected):
        raise ValueError("该批次原始消息已过期或不完整，不能以旧总结代替原文重新分析")
    value, rev = await analyze_rows(
        batch["group_id"], rows, db_path=db_path, bot_qq=bot_qq
    )
    value["reanalysis_of"] = batch_id
    await asyncio.to_thread(
        db.save_reanalysis, batch_id, value, rev, db_path, rows=rows
    )
    return value


async def recover_primary_summary(batch_id, *, db_path=db.DB_PATH, cache_path=None, bot_qq=None):
    """用原消息重分析兜底批次；没有主模型结果时保留兜底，不移动累计游标。"""
    claim = await asyncio.to_thread(db.claim_recovery, batch_id, db_path)
    if not claim:
        return False
    finished = False
    try:
        recalled = await asyncio.to_thread(chat_context.recalled_ids, claim['group_id'], cache_path)
        rows = [r for r in claim['rows'] if r['message_id'] not in recalled]
        if not rows:
            finished = True
            return False
        value, rev = await analyze_rows(claim['group_id'], rows, db_path=db_path,
                                        bot_qq=bot_qq, primary_only=True)
        value['primary_reanalysis_of'] = batch_id
        changed = await asyncio.to_thread(db.save_reanalysis, batch_id, value, rev, db_path,
                                          rows=rows, expected_result=claim['result'])
        finished = True
        return changed
    finally:
        await asyncio.to_thread(db.release_recovery, batch_id, claim['claim_token'], db_path,
                                finished=finished)


async def process_batch(batch_id, *, db_path=db.DB_PATH, cache_path=None, bot_qq=None):
    claim = await asyncio.to_thread(db.claim_batch, batch_id, db.LEASE_SECONDS, db_path)
    if claim is None:
        return await asyncio.to_thread(db.get_batch, batch_id, db_path)
    batch = await asyncio.to_thread(db.get_batch, batch_id, db_path)
    token = claim["token"]
    try:
        recalled = await asyncio.to_thread(
            chat_context.recalled_ids, batch["group_id"], cache_path
        )
        rows = [r for r in claim["rows"] if r["message_id"] not in recalled]
        state = await asyncio.to_thread(db.get_cycle, batch["group_id"], db_path)
        previous = json.loads(state["result"]) if state["result"] else None
        if rows:
            result, rev = await analyze_rows(
                batch["group_id"],
                rows,
                db_path=db_path,
                bot_qq=bot_qq,
                previous=previous,
            )
        else:
            rev = await asyncio.to_thread(db.revision, db_path)
            result = {
                "topics": [],
                "terms_to_check": [],
                "period": f"{datetime.fromtimestamp(batch['start_ts'], TZ):%m-%d %H:%M}—{datetime.fromtimestamp(batch['end_ts'], TZ):%H:%M}",
                "message_count": 0,
                "raw_message_count": 0,
                "context_revision": rev,
                "usage": [],
            }
        await asyncio.to_thread(
            db.finish_batch, batch_id, result, rev, token, db_path, rows=rows
        )
    except asyncio.CancelledError:
        await asyncio.to_thread(db.release_batch, batch_id, token, db_path)
        raise
    except Exception as exc:
        await asyncio.to_thread(
            db.fail_batch, batch_id, type(exc).__name__, token, db_path
        )
        raise
    return await asyncio.to_thread(db.get_batch, batch_id, db_path)


def cache_key(namespace, group_id, rev, rows):
    # 身份、时间和内容均参与缓存键；昵称或消息内容变化会使结果失效。
    return hashlib.sha256(
        json.dumps(
            [namespace, group_id, rev, rows], ensure_ascii=False, sort_keys=True
        ).encode()
    ).hexdigest()


async def query_summary(
    group_id, start, end, label, *, db_path=db.DB_PATH, cache_path=None, bot_qq=""
):
    await asyncio.to_thread(db.init_db, db_path)
    rows = await asyncio.to_thread(
        chat_context.list_messages, group_id, start, end, cache_path
    )
    rows = counted_rows(rows, bot_qq)
    if not rows:
        raise ValueError("该区间没有已缓存的群聊消息")
    await asyncio.to_thread(chat_context.observe_names, group_id, rows, db_path)
    rev = await asyncio.to_thread(db.revision, db_path)
    key = cache_key("query", group_id, rev, rows)
    cached = await asyncio.to_thread(db.query_cache_get, key, db_path)
    if cached:
        cached["cache_hit"] = True
        cached["period"] = label
        return cached
    ids = {r["message_id"] for r in rows}
    batches = []
    covered = set()
    for batch in await asyncio.to_thread(
        db.cached_batches, group_id, start, end, db_path
    ):
        mids = await asyncio.to_thread(db.member_ids, [batch["id"]], db_path)
        # 撤回或缺失的消息使这一批不再适用于当前窗口。
        if not mids.issubset(ids) or mids & covered:
            continue
        batches.append(batch)
        covered.update(mids)
    partials = [json.loads(b["result"]) for b in batches]
    stale_context = any(b["context_revision"] != rev for b in batches)
    tail = [r for r in rows if r["message_id"] not in covered]
    people = await asyncio.to_thread(chat_context.list_people, group_id, db_path)
    context = {
        **await asyncio.to_thread(db.load_context, db_path),
        "bot_commands": command_context(),
    }
    chunk_cached_count = 0
    for i in range(0, len(tail), db.BATCH_SIZE):
        chunk = tail[i : i + db.BATCH_SIZE]
        messages, names = prepare_messages(chunk, people, bot_qq)
        if messages:
            chunk_key = cache_key("chunk", group_id, rev, chunk)
            part = await asyncio.to_thread(db.query_cache_get, chunk_key, db_path)
            if part:
                chunk_cached_count += len(chunk)
            else:
                part = await summarize(
                    messages, names, label, review=False, context=context
                )
                await asyncio.to_thread(db.query_cache_save, chunk_key, part, db_path)
            partials.append(part)
    if not partials:
        raise ValueError("该区间没有可总结的文字消息")
    if len(partials) == 1 and not stale_context:
        result = dict(partials[0])
    else:
        # 旧批次只用已保存的结论重新解释，不重新发送整批原文。
        result, cost = await _complete(
            {
                "period": label,
                "context": context,
                "people": prepare_messages(rows, people, bot_qq)[1],
                "partial_summaries": [summary_for_model(part) for part in partials],
                "task": "合并这些按消息批次生成的总结，使用当前名词解释纠正简称，合并重复话题，最多6个主要主题。"
                "略过散乱、无主题的闲聊，不拼凑日常闲聊或其他话题等兜底主题；没有有效讨论时允许topics为空。"
                "若上下文有修改，以当前定义为准。证据id只能来自给定总结，不引入新事实。",
            }
        )
        valid = {
            str(mid)
            for part in partials
            for k in ("topics", "terms_to_check")
            for item in part.get(k, [])
            for mid in item.get("message_ids", [])
        }
        validate_result(result, valid)
        result["usage"] = [cost]
    result.update(
        period=label,
        message_count=len(prepare_messages(rows, people, bot_qq)[0]),
        raw_message_count=len(rows),
        cached_message_count=len(covered) + chunk_cached_count,
        context_revision=rev,
        cache_hit=False,
    )
    await asyncio.to_thread(
        db.record_unknown_terms, group_id, result, None, db_path, rows=rows
    )
    await asyncio.to_thread(db.query_cache_save, key, result, db_path)
    return result
