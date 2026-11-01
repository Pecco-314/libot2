"""歌曲、歌手、歌单查询与演唱识别。"""

from __future__ import annotations

from src.plugins.utils import send_foldable_message

from src.common.time_args import is_date_arg, parse_date

import asyncio
import re
from datetime import datetime

from nonebot.adapters.onebot.v11 import Bot, Event, Message, MessageSegment
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

from src.capture.guesser import guess_song
from src.common.song_time import parse_song_query_time
from src.common.song_preview import parse_preview_args, preview_cache, format_preview
from src.db.singer_alias import add_singer_alias
from src.plugins.command_registry import (
    now_playing_cmd,
    random_search_cmd,
    singer_alias_cmd,
    song_list_cmd,
    song_preview_cmd,
    song_search_cmd,
    song_singer_cmd,
)
from src.plugins.command_support import _resolve_live_session, logger
from src.plugins.utils import (
    get_query_room_id,
    group_manager_required,
    send_forward_message,
)
from src.render.song import (
    render_random_bedtime_song,
    render_random_song,
    render_song_list,
    render_songs_by_keyword,
    render_songs_by_singer,
)
from src.spider.wrapper import get_name_by_roomid


@song_search_cmd.handle()
async def handle_song_search(
    bot: Bot, event: Event, matcher: Matcher, arg=CommandArg()
):
    keyword = arg.extract_plain_text().strip()
    if not keyword:
        await matcher.finish("用法：/查歌曲 <歌名>")

    try:
        results = await render_songs_by_keyword(keyword)
    except Exception as e:
        logger.error(f"渲染歌曲卡片失败: {e}")
        await matcher.finish("图片渲染失败")

    if not results:
        await matcher.finish(f"未找到与“{keyword}”相关的演唱记录")

    # 1. 准备合并转发的消息节点
    forward_nodes = []

    # 第一条节点：文字汇总提示
    maxsize = 5
    if len(results) < maxsize:
        content = f"找到 {len(results)} 首与“{keyword}”相关的歌曲："
    else:
        content = f"找到 {len(results)} 首与“{keyword}”相关的歌曲（已达到搜索上限）："
    forward_nodes.append(
        {
            "type": "node",
            "data": {"name": "LiBot", "uin": bot.self_id, "content": content},
        }
    )

    # 后续节点：每首歌一张图片
    for i, res in enumerate(results, start=1):
        forward_nodes.append(
            {
                "type": "node",
                "data": {
                    "name": "LiBot",
                    "uin": bot.self_id,
                    "content": [
                        MessageSegment.text(f"{i}. {res['data']['title']}"),
                        MessageSegment.image(file=str(res["image_path"])),
                    ],
                },
            }
        )

    try:
        await send_forward_message(bot, event, forward_nodes)
    except Exception as e:
        logger.error(f"发送合并转发消息失败: {e}")


@song_list_cmd.handle()
async def handle_song_list(
    matcher: Matcher,
    bot: Bot,
    event: Event,
    arg=CommandArg(),
):
    raw_args = arg.extract_plain_text().strip().split()
    target_date = None
    mode = None
    seen_mode = False
    for value in raw_args:
        if value in {"按歌手", "按语言", "按歌名", "按次数"}:
            if seen_mode:
                await matcher.finish("只能指定一种歌单分组或排序方式")
            mode = value
            seen_mode = True
            continue
        if is_date_arg(value):
            if target_date is not None:
                await matcher.finish("只能指定一个日期")
            try:
                target_date = parse_date(value).date()
            except ValueError as exc:
                await matcher.finish(str(exc))
            continue
        await matcher.finish("用法：/查歌单 [日期] [按歌手/按语言/按歌名/按次数]")

    if mode is None:
        mode = "按顺序" if target_date is not None else "按次数"

    try:
        result = await render_song_list(target_date, mode)
    except Exception as exc:
        logger.exception("渲染歌单失败: %s", exc)
        await matcher.finish("图片渲染失败")

    if not result:
        scope = target_date.strftime("%Y-%m-%d") if target_date else "曲库中"
        await matcher.finish(f"{scope}没有找到歌单记录")

    date_prefix = f"{target_date:%Y-%m-%d}的" if target_date else ""
    summary = f"{date_prefix}歌单（{mode}）"
    nodes = [
        {
            "type": "node",
            "data": {
                "name": "LiBot",
                "uin": bot.self_id,
                "content": summary,
            },
        },
        *[
            {
                "type": "node",
                "data": {
                    "name": "LiBot",
                    "uin": bot.self_id,
                    "content": MessageSegment.image(file=str(image_path)),
                },
            }
            for image_path in result["image_paths"]
        ],
    ]
    try:
        await send_forward_message(bot, event, nodes)
    except Exception as exc:
        logger.error("发送歌单合并转发失败: %s", exc)
        await matcher.finish("歌单发送失败，请稍后再试")


@song_singer_cmd.handle()
async def handle_song_singer(matcher: Matcher, arg=CommandArg()):
    singer = arg.extract_plain_text().strip()
    if not singer:
        await matcher.finish("用法：/查歌手 <歌手名>")

    try:
        result = await render_songs_by_singer(singer)
    except Exception as exc:
        logger.error("渲染歌手歌曲列表失败: %s", exc)
        await matcher.finish("图片渲染失败")

    if not result:
        await matcher.finish(f"未找到与“{singer}”相关的歌曲")

    message = Message(
        [
            MessageSegment.text(f"歌手 {singer} 的歌曲列表："),
            MessageSegment.image(file=str(result["image_path"])),
        ]
    )
    await matcher.finish(message)


@singer_alias_cmd.handle()
@group_manager_required
async def handle_singer_alias(matcher: Matcher, event: Event, arg=CommandArg()):
    parts = [part.strip() for part in re.split(r"[|｜=＝]", arg.extract_plain_text())]
    if len(parts) != 2 or not all(parts):
        await matcher.finish(
            "用法：新增歌手别名 别名 | 完整歌手名，例如：邓紫棋 | G.E.M. 邓紫棋"
        )
    try:
        singer, changed = await asyncio.to_thread(add_singer_alias, *parts)
    except ValueError as exc:
        await matcher.finish(str(exc))
    await matcher.finish(
        f"{'已登记' if changed else '已可使用'}：{parts[0]} = {singer}"
    )


async def _handle_random_song(matcher: Matcher, count: str):
    lowest_count = 3
    if count.isdigit():
        lowest_count = int(count)

    try:
        result = await render_random_song(lowest_count)
    except Exception as e:
        logger.error(f"渲染歌曲卡片失败: {e}")
        await matcher.finish("图片渲染失败")

    if not result:
        await matcher.finish(f"未找到演唱次数大于等于{lowest_count}的歌曲")

    message = Message(
        [
            MessageSegment.text(f"随机抽取到歌曲：{result['data']['title']}"),
            MessageSegment.image(file=str(result["image_path"])),
        ]
    )

    await matcher.finish(message)


async def _handle_bedtime_song(matcher: Matcher):
    try:
        result = await render_random_bedtime_song()
    except Exception as exc:
        logger.error("渲染晚安曲卡片失败: %s", exc)
        await matcher.finish("图片渲染失败")

    if not result:
        await matcher.finish("暂时没有可抽取的晚安曲")

    message = Message(
        [
            MessageSegment.text(f"随机抽取到晚安曲：{result['data']['title']}"),
            MessageSegment.image(file=str(result["image_path"])),
        ]
    )
    await matcher.finish(message)


@random_search_cmd.handle()
async def handle_random_song(matcher: Matcher, arg=CommandArg()):
    await _handle_random_song(matcher, arg.extract_plain_text().strip())


@now_playing_cmd.handle()
async def handle_now_playing(matcher: Matcher, event: Event, arg=CommandArg()):
    room_id = get_query_room_id(event)

    try:
        target_ts = parse_song_query_time(arg.extract_plain_text())
    except ValueError as exc:
        await matcher.finish(str(exc))
        return
    try:
        results = guess_song(room_id, target_ts)
    except Exception as e:
        logger.error(f"歌曲匹配时发生异常: {e}")
        await matcher.finish("歌曲匹配时发生异常")
    if not results:
        await matcher.finish("未找到匹配的歌曲")
    message = "当前可能在唱的歌曲：\n"
    for i, res in enumerate(results, start=1):
        translation = (res.get("title_trans") or "").strip()
        title = f"{res['title']}（{translation}）" if translation else res['title']
        message += f"{i}. {title} - {res['singer']} ({res['final_score']:.2%})\n"
    await matcher.finish(message)


@song_preview_cmd.handle()
async def handle_song_preview(
    matcher: Matcher,
    bot: Bot,
    event: Event,
    arg=CommandArg(),
):
    room_id = get_query_room_id(event)
    try:
        date_str, session_index, threshold = parse_preview_args(arg.extract_plain_text())
        target_session, session_label, _ongoing = _resolve_live_session(
            room_id,
            date_str,
            session_index,
        )
    except ValueError as exc:
        await matcher.finish(str(exc))
        return

    start_ts = int(target_session["start_ts"])
    end_ts = target_session["end_ts"]
    await matcher.send(
        f"正在粗略识别{session_label}的歌单……\n"
        f"匹配分≥{threshold * 100:g}%，结合歌词覆盖与连续性筛选，请核对后生成。"
    )
    try:
        preview = await asyncio.to_thread(
            preview_cache.get,
            room_id,
            start_ts,
            end_ts,
            threshold,
        )
    except Exception as exc:
        logger.exception("生成预览歌单失败: %s", exc)
        await matcher.finish("预览歌单生成失败")
        return

    if preview['pending']:
        await matcher.finish("直播刚开始，等待至少2分钟的ASR记录后再试")
    if not preview['has_asr']:
        await matcher.finish("该场直播在可查询时段内没有 ASR 记录")
    entries = preview['entries']

    if not entries:
        await matcher.finish("该场直播没有识别到可能的歌曲")

    lines = format_preview(start_ts, entries)

    try:
        await send_foldable_message(bot, event, "\n".join(lines))
    except Exception as exc:
        logger.error("发送预览歌单合并转发失败: %s", exc)
        await matcher.finish("预览歌单发送失败，请稍后再试")
