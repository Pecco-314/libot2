"""缩写查询、提交与本地记录管理。"""

from __future__ import annotations

import asyncio
import shlex

from nonebot.adapters.onebot.v11 import Bot, Event, MessageSegment
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

from src.db.abbreviation import (
    add_abbreviation,
    is_valid_abbreviation,
    list_abbreviations,
    remove_abbreviation,
    remove_abbreviations,
)
from src.plugins.command_handlers.live import _check_modify_permission
from src.plugins.command_registry import (
    abbreviation_add_cmd,
    abbreviation_query_cmd,
    abbreviation_remove_cmd,
)
from src.plugins.command_support import logger
from src.plugins.utils import send_forward_message
from src.spider.nbnhhsh import NbnhhshError, guess_abbreviation, submit_translation


def _format_abbreviation_results(
    query: str,
    local_records: list[dict],
    remote_results: list[dict],
) -> str:
    groups: dict[str, dict[str, object]] = {}

    def ensure_group(name: str) -> dict[str, object]:
        key = name.casefold()
        if key not in groups:
            groups[key] = {"name": name, "items": [], "seen": set()}
        return groups[key]

    if local_records:
        local_group = ensure_group(query)
        local_items = local_group["items"]
        local_seen = local_group["seen"]
        assert isinstance(local_items, list)
        assert isinstance(local_seen, set)
        for record in local_records:
            expansion = str(record["expansion"])
            key = _normalize_abbreviation_expansion(expansion)
            if key in local_seen:
                continue
            local_seen.add(key)
            local_items.append(f"{expansion}(*)")

    for result in remote_results:
        name = str(result.get("name") or query).strip() or query
        group = ensure_group(name)
        items = group["items"]
        seen = group["seen"]
        assert isinstance(items, list)
        assert isinstance(seen, set)
        for value in result.get("expansions") or []:
            expansion = str(value).strip()
            key = _normalize_abbreviation_expansion(expansion)
            if not expansion or key in seen:
                continue
            seen.add(key)
            items.append(expansion)

    sections: list[str] = []
    for group in groups.values():
        items = group["items"]
        assert isinstance(items, list)
        if not items:
            continue
        sections.append(f"{group['name']} 可能是：{'，'.join(items)}")
    return "\n\n".join(sections)


def _normalize_abbreviation_expansion(value: object) -> str:
    return " ".join(str(value).strip().split()).casefold()


def _approved_local_record_ids(
    query: str,
    local_records: list[dict],
    remote_results: list[dict],
) -> list[int]:
    query_key = query.strip().casefold()
    approved: set[str] = set()
    for result in remote_results:
        if str(result.get("name") or "").strip().casefold() != query_key:
            continue
        approved.update(
            _normalize_abbreviation_expansion(value)
            for value in result.get("approved_expansions") or []
        )
    approved.discard("")
    return [
        int(record["id"])
        for record in local_records
        if _normalize_abbreviation_expansion(record["expansion"]) in approved
    ]


@abbreviation_query_cmd.handle()
async def handle_abbreviation_query(
    matcher: Matcher,
    bot: Bot,
    event: Event,
    arg=CommandArg(),
):
    query = arg.extract_plain_text().strip()
    if not query:
        await matcher.finish("用法：/查缩写 <缩写>")
    if len(query) > 100:
        await matcher.finish("缩写过长")

    local_records = await asyncio.to_thread(list_abbreviations, query)
    remote_results: list[dict] = []
    remote_failed = False
    try:
        remote_results = await guess_abbreviation(query)
    except NbnhhshError as exc:
        remote_failed = True
        logger.warning("查询能不能好好说话接口失败 query=%r error=%s", query, exc)

    approved_local_ids = _approved_local_record_ids(
        query,
        local_records,
        remote_results,
    )
    if approved_local_ids:
        removed_count = await asyncio.to_thread(
            remove_abbreviations,
            approved_local_ids,
        )
        if removed_count != len(approved_local_ids):
            logger.warning(
                "清理已审核缩写数量不符 query=%r expected=%d removed=%d",
                query,
                len(approved_local_ids),
                removed_count,
            )
        else:
            logger.info(
                "已清理审核通过的本地缩写 query=%r records=%s",
                query,
                approved_local_ids,
            )
        local_records = await asyncio.to_thread(list_abbreviations, query)

    text = _format_abbreviation_results(query, local_records, remote_results)
    if not text:
        if remote_failed:
            await matcher.finish("缩写接口暂时不可用，本地数据库中也没有相关记录")
        await matcher.finish(f"没有查到“{query}”的释义")
    if remote_failed:
        text += "\n\n（远程接口暂时不可用，以上为本地结果）"
    nodes = [
        {
            "type": "node",
            "data": {
                "name": "Libot",
                "uin": bot.self_id,
                "content": MessageSegment.text(text),
            },
        }
    ]
    try:
        await send_forward_message(bot, event, nodes)
    except Exception as exc:
        logger.error("发送缩写查询合并转发失败: %s", exc)
        await matcher.finish("缩写查询发送失败，请稍后再试")


@abbreviation_add_cmd.handle()
async def handle_abbreviation_add(
    matcher: Matcher,
    event: Event,
    arg=CommandArg(),
):
    try:
        parts = shlex.split(arg.extract_plain_text().strip())
    except ValueError:
        await matcher.finish("参数中的引号没有闭合")
    if len(parts) < 2:
        await matcher.finish("用法：/新增缩写 <缩写> <释义>")

    abbreviation = parts[0]
    expansion = " ".join(parts[1:])
    if not is_valid_abbreviation(abbreviation):
        await matcher.finish("缩写只能由2至64个英文字母或数字组成")
    if len(expansion) > 200:
        await matcher.finish("释义过长")

    added = await asyncio.to_thread(
        add_abbreviation,
        abbreviation,
        expansion,
        int(event.get_user_id()),
    )
    if not added:
        await matcher.finish("这条本地缩写记录已存在")

    local_message = f"已新增本地缩写：{abbreviation.casefold()} = {expansion}"
    try:
        await submit_translation(abbreviation, expansion)
    except NbnhhshError as exc:
        logger.warning(
            "提交缩写审核失败 abbreviation=%r expansion=%r error=%s",
            abbreviation,
            expansion,
            exc,
        )
        await matcher.finish(local_message + "\n提交网站审核失败，请稍后再试")
    await matcher.finish(local_message + "\n已自动提交网站审核")


@abbreviation_remove_cmd.handle()
async def handle_abbreviation_remove(
    matcher: Matcher,
    event: Event,
    arg=CommandArg(),
):
    try:
        parts = shlex.split(arg.extract_plain_text().strip())
    except ValueError:
        await matcher.finish("参数中的引号没有闭合")
    if not parts:
        await matcher.finish("用法：/删除缩写 <缩写> [释义]")

    abbreviation = parts[0]
    requested_expansion = " ".join(parts[1:]).strip()
    records = await asyncio.to_thread(list_abbreviations, abbreviation)
    if not records:
        await matcher.finish("本地数据库中没有这个缩写")

    if requested_expansion:
        normalized = " ".join(requested_expansion.split()).casefold()
        records = [
            record
            for record in records
            if " ".join(str(record["expansion"]).split()).casefold() == normalized
        ]
        if not records:
            await matcher.finish("本地数据库中没有这条缩写释义")
    elif len(records) > 1:
        choices = "\n".join(f"- {record['expansion']}" for record in records)
        await matcher.finish("本地存在多条释义，请指定要删除的完整释义：\n" + choices)

    record = records[0]
    if not await _check_modify_permission(record, event):
        await matcher.finish("权限不足：仅群管理员或该缩写的添加者可删除")

    removed = await asyncio.to_thread(remove_abbreviation, int(record["id"]))
    if not removed:
        await matcher.finish("系统错误，未能删除")
    await matcher.finish(
        f"已删除本地缩写：{record['abbreviation']} = {record['expansion']}"
    )
