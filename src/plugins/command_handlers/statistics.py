"""粉丝、舰长、粉丝团、同接、盲盒与营收统计。"""

from __future__ import annotations

import asyncio
import re
import shlex
from datetime import datetime

from nonebot.adapters.onebot.v11 import Bot, Event, Message, MessageSegment
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

from src.common.blind_box import BLIND_BOX_DATA_START
from src.common.stats_period import TZ as STATS_TZ, parse_stats_period
from src.db.event import list_heart_blind_box_events, list_online_counts
from src.db.fan_club import (
    latest_complete_snapshot,
    list_common_snapshot_members,
    list_snapshot_members,
    resolve_target,
)
from src.db.stats import get_stat_start_date
from src.plugins.command_registry import (
    blind_box_cmd,
    club_trend_cmd,
    concurrent_cmd,
    dc_cmd,
    fan_club_list_cmd,
    fans_trend_cmd,
    guards_trend_cmd,
)
from src.plugins.command_support import (
    _parse_concurrent_args,
    _resolve_live_session,
    logger,
)
from src.plugins.utils import get_query_room_id, send_forward_message
from src.render.blind_box import build_blind_box_rank_items, render_blind_box_rank
from src.render.dc import render_dc_images
from src.render.fan_club import render_common_fan_club_members, render_fan_club_members
from src.render.stats import (
    render_concurrent_trend,
    render_fan_club_trend,
    render_fans_trend,
    render_guards_trend,
)
from src.spider.wrapper import (
    get_fan_club_num,
    get_fans_num,
    get_guard_num,
    get_name_by_roomid,
    get_uid_by_roomid,
)


async def _handle_stats_query(
    matcher: Matcher, event: Event, arg: MessageSegment, stat_type: str
):
    room_id = get_query_room_id(event)

    try:
        period = parse_stats_period(arg.extract_plain_text())
    except ValueError as exc:
        await matcher.finish(str(exc))
        return
    stat_start_date = await asyncio.to_thread(get_stat_start_date, room_id, stat_type)

    uname = await get_name_by_roomid(room_id) or str(room_id)

    current_value: int | None = None
    if period.use_current:
        try:
            uid = await get_uid_by_roomid(room_id)
            if stat_type == "fans":
                current_value = await get_fans_num(uid)
            elif stat_type == "guards":
                current_value = await get_guard_num(room_id, uid)
            else:
                current_value = await get_fan_club_num(uid)
            if current_value is not None and current_value < 0:
                current_value = None
        except Exception as exc:
            logger.warning("查询实时%s数据失败: %s", stat_type, exc)

    range_args = dict(start_ts=period.start_ts, end_ts=period.end_ts, period_label=period.label)
    if stat_type == "fans":
        data = await render_fans_trend(room_id, period.days, uname, **range_args)
    elif stat_type == "guards":
        data = await render_guards_trend(room_id, period.days, uname, **range_args)
    else:
        data = await render_fan_club_trend(room_id, period.days, uname, **range_args)

    if not data:
        await matcher.finish(f"{period.label}内有效记录不足两条，无法生成趋势图")
        return

    recorded_value = data["end_value"]
    image_path = data["path"]

    stat_name = ""
    if stat_type == "fans":
        stat_name = "粉丝"
    elif stat_type == "guards":
        stat_name = "大航海"
    else:
        stat_name = "粉丝团"
    display_value = current_value if current_value is not None else recorded_value
    delta = display_value - data["begin_value"]
    text = f"{uname}的{stat_name}数：{display_value} ({delta:+})"
    query_start = datetime.fromtimestamp(period.start_ts, STATS_TZ).replace(tzinfo=None)
    if query_start < stat_start_date:
        text += f"\n（数据从{stat_start_date.strftime('%Y-%m-%d')}开始）"

    message = Message(
        [
            MessageSegment.text(text),
            MessageSegment.image(file=str(image_path)),
        ]
    )

    await matcher.finish(message)


@fans_trend_cmd.handle()
async def handle_fans_trend(matcher: Matcher, event: Event, arg=CommandArg()):
    await _handle_stats_query(matcher, event, arg, "fans")


@guards_trend_cmd.handle()
async def handle_guards_trend(matcher: Matcher, event: Event, arg=CommandArg()):
    await _handle_stats_query(matcher, event, arg, "guards")


@club_trend_cmd.handle()
async def handle_club_trend(matcher: Matcher, event: Event, arg=CommandArg()):
    await _handle_stats_query(matcher, event, arg, "club")


@fan_club_list_cmd.handle()
async def handle_fan_club_list(
    matcher: Matcher,
    bot: Bot,
    event: Event,
    arg=CommandArg(),
):
    query = arg.extract_plain_text().strip()
    try:
        queries = shlex.split(query) if query else ["2030198123"]
    except ValueError:
        await matcher.finish("参数中的引号没有闭合")
    if len(queries) > 5:
        await matcher.finish("一次最多查询5个粉丝团的交集")

    selected_targets: list[dict] = []
    for target_query in queries:
        matches = resolve_target(target_query)
        if not matches:
            await matcher.finish(f"没有找到粉丝团抓取对象：{target_query}")
        if len(matches) > 1:
            choices = "、".join(
                f"{target['full_name']}（{target['uid']}）" for target in matches
            )
            await matcher.finish(f"名称对应多个主播，请改用UID：{choices}")
        selected_targets.append(matches[0])

    target_uids = [int(target["uid"]) for target in selected_targets]
    if len(target_uids) != len(set(target_uids)):
        await matcher.finish("同一个主播不需要重复填写")

    snapshots: list[dict] = []
    for target in selected_targets:
        streamer_uid = int(target["uid"])
        snapshot = latest_complete_snapshot(streamer_uid)
        if snapshot is None:
            await matcher.finish(f"{target['short_name']}暂无可用的粉丝团数据")
        snapshots.append(snapshot)

    is_common_query = len(snapshots) >= 2
    if is_common_query:
        members = list_common_snapshot_members(
            [int(snapshot["id"]) for snapshot in snapshots]
        )
        render = render_common_fan_club_members
    else:
        members = list_snapshot_members(int(snapshots[0]["id"]))
        render = render_fan_club_members
    try:
        images = await asyncio.to_thread(
            render,
            snapshots if is_common_query else snapshots[0],
            members,
        )
    except Exception as exc:
        logger.exception("渲染粉丝团成员失败 uids=%s", target_uids)
        await matcher.finish(f"粉丝团成员渲染失败：{exc}")

    nodes = []
    if is_common_query:
        names = "、".join(str(target["short_name"]) for target in selected_targets)
        nodes.append(
            {
                "type": "node",
                "data": {
                    "name": "Libot",
                    "uin": bot.self_id,
                    "content": MessageSegment.text(f"{names}的共同粉丝团有："),
                },
            }
        )
    nodes.extend(
        [
            {
                "type": "node",
                "data": {
                    "name": "Libot",
                    "uin": bot.self_id,
                    "content": MessageSegment.image(file=str(image_path)),
                },
            }
            for image_path in images
        ]
    )
    try:
        await send_forward_message(bot, event, nodes)
    except Exception as exc:
        logger.error("发送粉丝团成员合并转发失败: %s", exc)
        await matcher.finish("粉丝团成员发送失败，请稍后再试")


@concurrent_cmd.handle()
async def handle_concurrent(matcher: Matcher, event: Event, arg=CommandArg()):
    room_id = get_query_room_id(event)

    raw = arg.extract_plain_text()
    date_str, session_index = _parse_concurrent_args(raw)

    try:
        target_session, session_label, ongoing = _resolve_live_session(
            room_id, date_str, session_index
        )
    except ValueError as exc:
        await matcher.finish(str(exc))

    start_ts = int(target_session["start_ts"])
    end_ts = target_session["end_ts"]
    if end_ts is None:
        end_ts = int(datetime.now().timestamp())
    end_ts = int(end_ts)

    records = list_online_counts(room_id, start_ts, end_ts)
    if not records or len(records) < 2:
        await matcher.finish("同接数据不足")

    times = [datetime.fromtimestamp(r["timestamp"]) for r in records]
    values = [int(r["count"]) for r in records]

    uname = await get_name_by_roomid(room_id) or str(room_id)

    title = f"{uname} 同接趋势（{session_label}）"
    chart = await render_concurrent_trend(times, values, title)
    if not chart:
        await matcher.finish("数据不足，生成失败")

    avg_value = int(round(sum(values) / len(values)))
    max_value = max(values)
    message_text = f"{session_label}平均同接：{avg_value}，最高同接：{max_value}"
    if session_label == "当前直播":
        message_text = f"{message_text}，当前同接：{values[-1]}"

    message = Message(
        [
            MessageSegment.text(message_text),
            MessageSegment.image(file=str(chart["path"])),
        ]
    )
    await matcher.finish(message)


def _resolve_blind_box_range(
    room_id: int,
    raw_arg: str,
) -> tuple[int, int, str]:
    raw_arg = raw_arg.strip()
    now = datetime.now()
    data_start = datetime.strptime(BLIND_BOX_DATA_START, "%Y-%m-%d")

    if re.fullmatch(r"\d{4}", raw_arg):
        year = int(raw_arg)
        period_start = datetime(year, 1, 1)
        period_end = datetime(year + 1, 1, 1)
        query_start = max(period_start, data_start)
        query_end = min(period_end, now)
        if query_start >= query_end:
            raise ValueError(
                f"没有可查询的数据，心动盲盒数据始于 {BLIND_BOX_DATA_START}"
            )
        label = f"{year}年"
        if query_start > period_start:
            label += f"（统计始于{query_start:%m-%d}）"
        end_ts = int(query_end.timestamp())
        if query_end == period_end:
            end_ts -= 1
        return int(query_start.timestamp()), end_ts, label

    month_match = re.fullmatch(r"(\d{4})-?(\d{2})", raw_arg)
    if month_match:
        year, month = map(int, month_match.groups())
        if not 1 <= month <= 12:
            raise ValueError("月份格式错误，正确格式：YYYY-MM")
        period_start = datetime(year, month, 1)
        period_end = (
            datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
        )
        query_start = max(period_start, data_start)
        query_end = min(period_end, now)
        if query_start >= query_end:
            raise ValueError(
                f"没有可查询的数据，心动盲盒数据始于 {BLIND_BOX_DATA_START}"
            )
        label = f"{year}年{month:02d}月"
        if query_start > period_start:
            label += f"（统计始于{query_start:%m-%d}）"
        end_ts = int(query_end.timestamp())
        if query_end == period_end:
            end_ts -= 1
        return int(query_start.timestamp()), end_ts, label

    date_str, session_index = _parse_concurrent_args(raw_arg)
    target_session, session_label, _ongoing = _resolve_live_session(
        room_id, date_str, session_index
    )
    return (
        int(target_session["start_ts"]),
        int(target_session.get("end_ts") or now.timestamp()),
        session_label,
    )


@blind_box_cmd.handle()
async def handle_blind_box(
    bot: Bot,
    matcher: Matcher,
    event: Event,
    arg=CommandArg(),
):
    room_id = get_query_room_id(event)
    try:
        start_ts, end_ts, session_label = _resolve_blind_box_range(
            room_id, arg.extract_plain_text()
        )
    except ValueError as exc:
        await matcher.finish(str(exc))

    rows = list_heart_blind_box_events(room_id, start_ts, end_ts)
    items = build_blind_box_rank_items(rows)
    if not items:
        await matcher.finish(f"{session_label}没有找到心动盲盒记录")

    try:
        results = await render_blind_box_rank(
            f"{session_label} · 心动盲盒榜",
            items,
            page_size=15,
        )
    except Exception as exc:
        logger.error("渲染心动盲盒榜失败: %s", exc)
        await matcher.finish("心动盲盒榜渲染失败，请稍后再试")

    if len(results) == 1:
        await matcher.finish(MessageSegment.image(file=str(results[0]["image_path"])))

    nodes = [
        {
            "type": "node",
            "data": {
                "name": "Libot",
                "uin": bot.self_id,
                "content": MessageSegment.image(file=str(result["image_path"])),
            },
        }
        for result in results
    ]
    try:
        await send_forward_message(bot, event, nodes)
    except Exception as exc:
        logger.error("发送心动盲盒榜合并转发失败: %s", exc)
        await matcher.finish("心动盲盒榜发送失败，请稍后再试")


@dc_cmd.handle()
async def handle_dc(matcher: Matcher, bot: Bot, event: Event, arg=CommandArg()):
    args = arg.extract_plain_text().strip().split()

    filter_type = "vr"
    time_args: list[str] = []

    for a in args:
        a_upper = a.upper()
        if a_upper in ["VR", "PSP", "VRPSP", "ALL"]:
            filter_type = a_upper.lower()
            if filter_type == "vrpsp":
                filter_type = "all"
        elif re.match(r"^20\d{2}$", a):  # 例如 2026
            time_args.append(a)
        elif re.match(r"^20\d{2}-(0[1-9]|1[0-2])$", a):  # 例如 2026-06
            time_args.append(a)
        elif re.match(r"^20\d{2}(0[1-9]|1[0-2])$", a):  # 兼容 202606 这种连续格式
            time_args.append(f"{a[:4]}-{a[4:]}")
        else:
            await matcher.finish("用法：/斗虫 [VR/PSP/ALL] [月份/年份] [结束月份]")

    if len(time_args) > 2:
        await matcher.finish("斗虫最多填写起始、结束两个月份")
    if len(time_args) == 2 and any(len(value) != 7 for value in time_args):
        await matcher.finish(
            "范围查询需要填写两个完整月份，例如：/斗虫 2026-06 2026-08"
        )

    time_str = time_args[0] if time_args else datetime.now().strftime("%Y-%m")
    end_time_str = time_args[1] if len(time_args) == 2 else None
    if end_time_str is not None and time_str > end_time_str:
        await matcher.finish("斗虫统计的起始月份不能晚于结束月份")
    period_label = (
        f"{time_str}～{end_time_str}" if end_time_str is not None else time_str
    )

    try:
        images = await render_dc_images(
            filter_type,
            time_str,
            end_time_str=end_time_str,
        )
    except Exception as e:
        logger.error(f"渲染斗虫图片失败: {e}")
        await matcher.finish("数据获取或图片渲染失败，请稍后再试。")

    if not images:
        await matcher.finish(
            f"未找到 {filter_type.upper()} 在 {period_label} 的相关营收数据"
        )

    # 构造合并转发节点
    nodes = []
    info = f"统计区间：{period_label} 社团：{filter_type.upper()}"
    nodes.append(
        {"type": "node", "data": {"name": "LiBot", "uin": bot.self_id, "content": info}}
    )
    for img in images:
        nodes.append(
            {
                "type": "node",
                "data": {
                    "name": "Libot",
                    "uin": bot.self_id,
                    "content": MessageSegment.image(file=str(img)),
                },
            }
        )

    try:
        await send_forward_message(bot, event, nodes)
    except Exception as e:
        logger.error(f"发送转发消息失败: {e}")
