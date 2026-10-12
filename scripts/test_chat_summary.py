#!/usr/bin/env python3
"""只生成本地群聊总结实验报告，不发送群消息或重启服务。"""
# ruff: noqa: E402

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.common.utils import load_env_file
from src.common.chat_summary import (
    TZ,
    parse_period,
    prepare_messages,
    format_summary,
    correct_generation_labels,
    summary_for_model,
)
from src.common.chat_summary_commands import command_context
from src.common.chat_summary_payload import compact_payload, wire_message
from src.db import chat_context
from src.db import chat_summary as summary_db
from src.common.chat_summary_service import (
    stage_ready,
    process_batch,
    query_summary,
    reanalyze_batch,
    hydrate_participants,
    cached_count_summary,
)


async def run(args):
    load_env_file()
    if args.backend:
        os.environ['AI_BACKEND'] = args.backend
    if args.pc_bridge:
        os.environ['AI_CODEX_USE_PC_BRIDGE'] = 'true'
    summary_db.init_db()
    if args.fix_generations:
        changed = []
        for batch in summary_db.cached_batches(args.group, 0, int(time.time()) + 1):
            value = json.loads(batch['result'])
            before = json.dumps(value, ensure_ascii=False, sort_keys=True)
            correct_generation_labels(value)
            if before != json.dumps(value, ensure_ascii=False, sort_keys=True):
                summary_db.save_reanalysis(batch['id'], value, summary_db.revision())
                changed.append(batch['id'])
        print(json.dumps({'corrected_batches': changed, 'model_calls': 0}, ensure_ascii=False), flush=True)
        return
    if args.backfill_mentions:
        print(
            summary_db.backfill_term_mentions(bot_qq=os.getenv("BOT_QQ", "")),
            flush=True,
        )
        print(
            json.dumps(
                [
                    {"term": row["term"], "mentions": row["mention_count"]}
                    for row in summary_db.unknown_page()["items"][:15]
                ],
                ensure_ascii=False,
            ),
            flush=True,
        )
        return
    now = datetime.now(TZ)
    output = ROOT / "data" / "reports" / "chat_summary" / now.strftime("%Y%m%d_%H%M%S")
    output.mkdir(parents=True)
    if args.fresh_batch:
        from src.common.chat_summary import summarize, attach_participants
        from src.render.chat_summary import render_summary
        from src.common.qq_avatars import ensure_avatars

        for bid in args.fresh_batch:
            batch = summary_db.get_batch(bid)
            if not batch or batch['status'] != 'complete':
                raise ValueError('只能实验已完成的批次')
            with summary_db.connection() as conn:
                expected = {(r[0], r[1]) for r in conn.execute(
                    'SELECT message_id,sent_at FROM chat_summary_member WHERE batch_id=?', (bid,))}
            recalled = chat_context.recalled_ids(batch['group_id'])
            expected = {(mid, ts) for mid, ts in expected if mid not in recalled}
            rows = [r for r in chat_context.list_messages(batch['group_id'], batch['start_ts'], batch['end_ts'] + 1)
                    if (r['message_id'], r['sent_at']) in expected]
            if not rows or len(rows) != len(expected):
                raise ValueError('原始消息已过期或不完整')
            people = chat_context.list_people(batch['group_id'])
            messages, identities = prepare_messages(rows, people, os.getenv('BOT_QQ', ''))
            start, end = datetime.fromtimestamp(batch['start_ts'], TZ), datetime.fromtimestamp(batch['end_ts'], TZ)
            label = f'{start:%m-%d %H:%M}—{end:%H:%M}'
            print(f'fresh batch {bid}: {label}, {len(rows)} raw / {len(messages)} text', flush=True)
            began = time.monotonic()
            value = await summarize(messages, identities, label, context=summary_db.load_context(group_id=batch['group_id']),
                                    review=False, previous_summary=None,
                                    primary_only=os.getenv('AI_BACKEND') == 'codex')
            attach_participants(value, rows, people, os.getenv('BOT_QQ', ''))
            value.update(experiment_of=bid, raw_message_count=len(rows),
                         input_policy='原消息与现有词表；不提供旧摘要或针对具体事件的额外说明')
            path = output / f'fresh_{bid}.json'
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2))
            print(format_summary(value), flush=True)
            print('seconds:', round(time.monotonic() - began, 2), 'usage:', value['usage'], flush=True)
            avatars = await ensure_avatars(p['qq'] for t in value['topics'] for p in t.get('participants', []))
            images = await asyncio.to_thread(render_summary, value, avatars, output / str(bid))
            print('report:', path, 'images:', [str(p) for p in images], flush=True)
        return
    if args.measure_payload:
        rows = chat_context.list_messages(args.group, int(time.time()) - summary_db.RAW_TTL, int(time.time()) + 1)
        rows = rows[-500:]
        people = chat_context.list_people(args.group)
        messages, identities = prepare_messages(rows, people, os.getenv('BOT_QQ', ''))
        payload = {
            'period': '最近500条',
            'context': {**summary_db.load_context(group_id=args.group), 'bot_commands': command_context()},
            'people': identities,
            'messages': [{k: v for k, v in m.items()
                          if not (k == 'nearby_unread_image' and not v)
                          and not (k == 'name' and m.get('user') in identities)} for m in messages],
            'previous_summary': summary_for_model(json.loads(summary_db.get_cycle(args.group)['result'] or 'null')),
        }
        old_size = len(json.dumps(payload, ensure_ascii=False))
        packed, _ = compact_payload(payload)
        new_size = len(json.dumps(packed, ensure_ascii=False, separators=(',', ':')))
        metrics = {'raw_messages': len(rows), 'text_messages': len(messages),
                   'json_characters_before': old_size, 'json_characters_after': new_size,
                   'reduction_percent': round((1 - new_size / old_size) * 100, 1),
                   'old_message_characters': sum(len(json.dumps(m, ensure_ascii=False)) for m in payload['messages']),
                   'new_message_characters': sum(len(json.dumps(wire_message(m, identities), ensure_ascii=False, separators=(',', ':'))) for m in messages),
                   'model_calls': 0}
        path = output / 'payload_metrics.json'
        path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2))
        print(json.dumps(metrics, ensure_ascii=False), flush=True)
        print(f'report: {path}', flush=True)
        return
    if args.prewarm:
        from src.render.chat_summary import render_summary_images

        batches = summary_db.cached_batches(args.group, 0, int(time.time()) + 1)
        report = {"batches": [], "ranges": [], "model_calls": 0}
        for batch in batches:
            value = json.loads(batch['result'])
            await hydrate_participants(args.group, value, batch['id'])
            paths = await render_summary_images(value)
            report['batches'].append({
                'id': batch['id'], 'messages': batch['message_count'],
                'images': [str(p) for p in paths],
            })
            print(f"prepared batch {batch['id']}: {len(paths)} images", flush=True)
        for count in range(500, 5001, 500):
            value = await cached_count_summary(args.group, count)
            paths = await render_summary_images(value)
            report['ranges'].append({
                'requested': count, 'actual': value['raw_message_count'],
                'batch_ids': value['batch_ids'], 'images': [str(p) for p in paths],
            })
            print(f"prepared {count}: {len(paths)} images", flush=True)
        path = output / 'prewarm.json'
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"report: {path}", flush=True)
        return
    if args.render_batch:
        batch = summary_db.get_batch(args.render_batch)
        if not batch or not batch["result"]:
            raise ValueError("批次尚无总结")
        value = json.loads(batch["result"])
        await hydrate_participants(batch["group_id"], value, batch["id"])
        from src.common.qq_avatars import ensure_avatars
        from src.render.chat_summary import render_summary

        avatars = await ensure_avatars(
            p["qq"]
            for topic in value.get("topics", [])
            for p in topic.get("participants", [])
        )
        paths = await asyncio.to_thread(render_summary, value, avatars, output)
        print(
            json.dumps(
                {"images": [str(p) for p in paths], "cached_avatars": len(avatars)},
                ensure_ascii=False,
            ),
            flush=True,
        )
        return
    if args.reanalyze_batch:
        old = summary_db.get_batch(args.reanalyze_batch)
        if not old:
            raise ValueError("批次不存在")
        before = json.loads(old["result"])
        began = time.monotonic()
        result = await reanalyze_batch(args.reanalyze_batch)
        path = output / f"reanalysis_{args.reanalyze_batch}.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(format_summary(result), flush=True)
        print(
            "context revision:",
            result["context_revision"],
            "seconds:",
            round(time.monotonic() - began, 2),
            flush=True,
        )
        print(
            "terms before:",
            len(before["terms_to_check"]),
            "after:",
            len(result["terms_to_check"]),
            flush=True,
        )
        print(
            "terms:",
            json.dumps(result["terms_to_check"], ensure_ascii=False),
            flush=True,
        )
        print("usage:", result["usage"], "report:", path, flush=True)
        return
    if args.batch:
        staged = stage_ready(args.group)
        print("staged:", staged, flush=True)
        ready = summary_db.ready_batch_ids()
        if args.latest:
            with summary_db.connection() as conn:
                ready = [
                    r[0]
                    for r in conn.execute(
                        "SELECT id FROM chat_summary_batch WHERE group_id=? AND status='pending' ORDER BY start_ts DESC LIMIT 1",
                        (args.group,),
                    )
                ]
        if not ready:
            print("没有待处理的完整500条批次", flush=True)
            return
        print("processing one batch:", ready[0], flush=True)
        began = time.monotonic()
        batch = await process_batch(ready[0])
        print("batch seconds:", round(time.monotonic() - began, 2), flush=True)
        if batch["status"] != "complete":
            print("batch status:", batch["status"], flush=True)
            return
        result = json.loads(batch["result"])
        path = output / "batch.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(format_summary(result), flush=True)
        print("report:", path, flush=True)
        current_rows = chat_context.list_messages(
            args.group, batch["start_ts"], batch["end_ts"] + 1
        )
        expected_ids = summary_db.member_ids([batch["id"]])
        if {r["message_id"] for r in current_rows} != expected_ids:
            print(
                "窗口边界含额外消息或有撤回，跳过真实API追加调用；批次已保存。",
                flush=True,
            )
            return
        began = time.monotonic()
        cached = await query_summary(
            args.group,
            batch["start_ts"],
            batch["end_ts"] + 1,
            result["period"],
            bot_qq=os.getenv("BOT_QQ", ""),
        )
        second = await query_summary(
            args.group,
            batch["start_ts"],
            batch["end_ts"] + 1,
            result["period"],
            bot_qq=os.getenv("BOT_QQ", ""),
        )
        print(
            "batch reused:",
            cached["cached_message_count"],
            "second query cache hit:",
            second["cache_hit"],
            flush=True,
        )
        print(
            "two cached queries seconds:",
            round(time.monotonic() - began, 3),
            flush=True,
        )
        return
    periods = args.period or ["30分钟", "1小时", "2小时"]
    for index, period in enumerate(periods, 1):
        start, end, label = parse_period(period, now)
        rows = chat_context.list_messages(args.group, start, end)
        chat_context.observe_names(args.group, rows)
        people = chat_context.list_people(args.group)
        messages, names = prepare_messages(rows, people, os.getenv("BOT_QQ", ""))
        print(f"testing {label}: {len(messages)} messages", flush=True)
        if not messages:
            print("no text messages", flush=True)
            continue
        try:
            result = await query_summary(
                args.group, start, end, label, bot_qq=os.getenv("BOT_QQ", "")
            )
        except Exception as exc:
            error = {
                "group_id": args.group,
                "period": label,
                "error": type(exc).__name__,
            }
            (output / f"{index:02d}_error.json").write_text(
                json.dumps(error, ensure_ascii=False, indent=2)
            )
            print(
                f"failed: {type(exc).__name__}; continuing with next period", flush=True
            )
            continue
        result["group_id"] = args.group
        # 报告仅保留结论、消息id和待确认术语，不长期复制聊天原文。
        path = output / f"{index:02d}.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(format_summary(result), flush=True)
        print(
            "terms:",
            json.dumps(result["terms_to_check"], ensure_ascii=False),
            flush=True,
        )
        print("usage:", result["usage"], "report:", path, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", type=int, default=605275779)
    parser.add_argument('--backend', choices=('qwen', 'codex'), help='仅本次实验选择模型后端')
    parser.add_argument('--pc-bridge', action='store_true', help='本次实验使用现有PC转发')
    parser.add_argument('--fresh-batch', type=int, action='append',
                        help='仅用指定批次原消息重新实验，不提供旧总结，不更新线上摘要；可多次指定')
    parser.add_argument('--prewarm', action='store_true',
                        help='仅预处理已有摘要的头像与500—5000条图片缓存，不调用模型')
    parser.add_argument('--fix-generations', action='store_true',
                        help='订正缓存中197/257/277/287/297被写成期数的错误，保留原摘要版本')
    parser.add_argument('--measure-payload', action='store_true',
                        help='仅比较最新500条的请求格式压缩大小，不调用模型')
    parser.add_argument(
        "--render-batch", type=int, help="只渲染指定缓存总结及参与人头像，不调用模型"
    )
    parser.add_argument(
        "--backfill-mentions",
        action="store_true",
        help="从现有原文缓存回补待解释词的提及条数，不调用API",
    )
    parser.add_argument(
        "--period",
        action="append",
        help="可重复指定，例如 --period 1小时 --period '00:00 01:00'",
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help="累计已有缓存，只处理一个500条批次并验证缓存复用",
    )
    parser.add_argument(
        "--latest", action="store_true", help="批次实验选择最新的完整批次"
    )
    parser.add_argument(
        "--reanalyze-batch",
        type=int,
        help="使用当前词表重分析指定已保存批次，保留旧版本",
    )
    args = parser.parse_args()
    async def run_and_close():
        try:
            await run(args)
        finally:
            if os.environ.get('AI_BACKEND') == 'codex':
                from src.common.chat_summary_codex import get_client
                await get_client().close()
    asyncio.run(run_and_close())


if __name__ == "__main__":
    main()
