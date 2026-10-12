"""歌曲、歌手、歌单查询与演唱识别。"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime

from nonebot.adapters.onebot.v11 import Bot, Event, Message, MessageSegment
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

from src.capture.guesser import collapse_song_timeline, guess_song, guess_song_timeline
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
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            if target_date is not None:
                await matcher.finish("只能指定一个日期")
            try:
                target_date = datetime.strptime(value, "%Y-%m-%d").date()
            except ValueError:
                await matcher.finish("日期格式错误，正确格式：YYYY-MM-DD")
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

    time_str = arg.extract_plain_text().strip()
    if time_str:
        try:
            target_ts = int(
                datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S").timestamp()
            )
        except ValueError:
            await matcher.finish("日期时间格式错误，正确格式：YYYY-MM-DD HH:MM:SS")
    else:
        target_ts = int(datetime.now().timestamp())
    try:
        results = guess_song(room_id, target_ts)
    except Exception as e:
        logger.error(f"歌曲匹配时发生异常: {e}")
        await matcher.finish("歌曲匹配时发生异常")
    if not results:
        await matcher.finish("未找到匹配的歌曲")
    message = "当前可能在唱的歌曲：\n"
    for i, res in enumerate(results, start=1):
        message += f"{i}. {res['title']} - {res['singer']} ({res['final_score']:.2%})\n"
    await matcher.finish(message)


@song_preview_cmd.handle()
@group_manager_required
async def handle_song_preview(
    matcher: Matcher,
    bot: Bot,
    event: Event,
    arg=CommandArg(),
):
    room_id = get_query_room_id(event)
    parts = [part for part in arg.extract_plain_text().strip().split() if part]
    if len(parts) > 2 or (len(parts) == 2 and not parts[1].isdigit()):
        await matcher.finish("用法：/预览歌单 [日期] [场次]")
    date_str = parts[0] if parts else None
    session_index = int(parts[1]) if len(parts) == 2 else None
    try:
        target_session, session_label, _ongoing = _resolve_live_session(
            room_id,
            date_str,
            session_index,
        )
    except ValueError as exc:
        await matcher.finish(str(exc))

    start_ts = int(target_session["start_ts"])
    end_ts = target_session["end_ts"]
    if end_ts is None:
        end_ts = int(datetime.now().timestamp())
    end_ts = int(end_ts)

    await matcher.send(f"正在粗略识别{session_label}的歌单……")
    try:
        timeline = await asyncio.to_thread(
            guess_song_timeline,
            room_id,
            start_ts,
            end_ts,
        )
        if not timeline:
            await matcher.finish("该场直播没有 ASR 记录")

        uname = await get_name_by_roomid(room_id) or str(room_id)
        entries = collapse_song_timeline(timeline, min_score=0.9)
    except Exception as exc:
        logger.exception("生成预览歌单失败: %s", exc)
        await matcher.finish("预览歌单生成失败")

    if not entries:
        await matcher.finish("该场直播没有识别到可能的歌曲")

    start_dt = datetime.fromtimestamp(start_ts)
    end_dt = datetime.fromtimestamp(end_ts)
    lines = [
        f"{uname} · {session_label}",
        f"{start_dt:%Y-%m-%d %H:%M:%S} — {end_dt:%Y-%m-%d %H:%M:%S}",
        f"粗略识别到 {len(entries)} 段歌曲：",
        "",
    ]
    for entry in entries:
        detected_at = datetime.fromtimestamp(int(entry["first_ts"]))
        lines.append(
            f"{detected_at:%Y-%m-%d %H:%M:%S}  {entry['title']} - {entry['singer']}"
        )
    lines.extend(
        [
            "",
            "ASR 粗略识别；仅保留置信度>90%的结果。60 秒窗口互不重叠，"
            "仅合并相邻相同结果，无法区分放歌与演唱。",
        ]
    )

    chunks: list[str] = []
    current_lines: list[str] = []
    current_length = 0
    for line in lines:
        added_length = len(line) + (1 if current_lines else 0)
        if current_lines and current_length + added_length > 3000:
            chunks.append("\n".join(current_lines))
            current_lines = []
            current_length = 0
        current_lines.append(line)
        current_length += len(line) + (1 if len(current_lines) > 1 else 0)
    if current_lines:
        chunks.append("\n".join(current_lines))

    if len(chunks) == 1:
        await matcher.finish(chunks[0])

    nodes = [
        {
            "type": "node",
            "data": {
                "name": "LiBot",
                "uin": bot.self_id,
                "content": MessageSegment.text(chunk),
            },
        }
        for chunk in chunks
    ]
    try:
        await send_forward_message(bot, event, nodes)
    except Exception as exc:
        logger.error("发送预览歌单合并转发失败: %s", exc)
        await matcher.finish("预览歌单发送失败，请稍后再试")
