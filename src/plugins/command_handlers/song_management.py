"""歌曲资料和演唱歌单录入。"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta

from nonebot.adapters.onebot.v11 import Bot, Event, GroupMessageEvent, Message
from nonebot.params import CommandArg

from src.db.song_list import add_new_song, add_song_record, search_songs_by_title
from src.plugins.command_registry import (
    cmd_add_song,
    cmd_generate_list,
    cmd_update_list,
)
from src.plugins.command_support import logger
from src.plugins.utils import group_manager_required
from src.spider.jobs.lyrics import sync_and_clean_lyrics


@cmd_add_song.handle()
@group_manager_required
async def handle_add_song(event: Event, arg: Message = CommandArg()):
    raw_text = arg.extract_plain_text().strip()
    if not raw_text:
        await cmd_add_song.finish(
            "请提供歌曲信息，参数使用 | 隔开，格式如下：\n"
            "新增歌曲 歌名 | 歌手 | 语言 | 翻译名(可选)"
        )

    parts = [p.strip() for p in re.split(r"[|｜]", raw_text)]
    title = parts[0]

    if not title:
        await cmd_add_song.finish("新增失败：歌名不能为空！")

    singer = parts[1] if len(parts) > 1 else ""
    language = parts[2] if len(parts) > 2 else ""
    title_trans = parts[3] if len(parts) > 3 else ""

    try:
        add_new_song(title, singer, language, title_trans)
        await cmd_add_song.send(f"已成功添加新歌：{title}")
        asyncio.create_task(sync_and_clean_lyrics())
    except Exception as e:
        await cmd_add_song.finish(f"添加新歌失败：{e}")


@cmd_generate_list.handle()
async def handle_generate_list(event: GroupMessageEvent, arg: Message = CommandArg()):
    raw_text = arg.extract_plain_text().strip()

    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    if not lines:
        await cmd_generate_list.finish(
            "请提供歌单文本，每行一首。\n"
            "例如：\n/生成歌单 2025-03-01\n南风过隙\nLove 2000"
        )

    record_date = datetime.now().strftime("%Y-%m-%d")
    first_line = lines[0]
    is_date_line = False

    # 智能解析首行的日期
    if first_line in ["今天", "今日"]:
        record_date = datetime.now().strftime("%Y-%m-%d")
        is_date_line = True
    elif first_line in ["昨天", "昨日"]:
        record_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        is_date_line = True
    elif first_line in ["前天"]:
        record_date = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
        is_date_line = True
    else:
        # 正则匹配形如 2026-07-15, 2026/07/15, 07-15, 07/15, 7.15 等格式
        date_match = re.match(
            r"^(?:(20\d{2})[-/.])?(1[0-2]|0?[1-9])[-/.]([12]\d|3[01]|0?[1-9])$",
            first_line,
        )
        if date_match:
            year_str = date_match.group(1)
            month = int(date_match.group(2))
            day = int(date_match.group(3))

            if year_str:
                year = int(year_str)
            else:
                year = datetime.now().year
                # 跨年处理：如果当前是1月，输入的月份是12月，自动推断为去年
                if datetime.now().month == 1 and month == 12:
                    year -= 1

            record_date = f"{year}-{month:02d}-{day:02d}"
            is_date_line = True

    # 如果第一行是日期，将其从歌单中移除
    if is_date_line:
        lines.pop(0)

    if not lines:
        await cmd_generate_list.finish(
            "请提供歌单文本，每行一首。\n"
            "例如：\n/生成歌单 2025-03-01\n南风过隙\nLove 2000"
        )

    # 继续生成草稿
    result_lines = [f"[{record_date}]"]

    for line in lines:
        clean_line = re.sub(r"^\d+[\.、]\s*", "", line)

        # 兼容中英文竖杠，分割出歌名和可选的歌手
        parts = [p.strip() for p in re.split(r"[|｜]", clean_line)]
        search_title = parts[0]
        search_singer = parts[1] if len(parts) > 1 else None

        # 携带可选的歌手参数进行精确防碰撞搜索
        search_res = search_songs_by_title(search_title, singer=search_singer, limit=1)
        if search_res:
            song = search_res[0]
            result_lines.append(
                f"{song['id']} # {song['title']} - {song['original_singer']}"
            )
        else:
            result_lines.append(f"? # {line} (未找到歌曲)")

    reply_msg = "\n".join(result_lines)

    await cmd_generate_list.finish(reply_msg)


@cmd_update_list.handle()
async def handle_update_list(bot: Bot, event: GroupMessageEvent, arg: Message = CommandArg()):
    text = arg.extract_plain_text().strip()

    if event.reply:
        reply_text = event.reply.message.extract_plain_text().strip()
        text = reply_text + "\n" + text

    if not text:
        await cmd_update_list.finish("请回复或提供歌单文本。")

    lines = [line.strip() for line in text.split("\n") if line.strip()]
    record_date = datetime.now().strftime("%Y-%m-%d")
    added_records = 0
    errors = []

    for line in lines:
        if line.startswith(("已生成歌单", "如需修改")):
            continue

        # 匹配日期，例如：[2026-07-15]
        date_match = re.match(r"^\[(\d{4}-\d{2}-\d{2})\]", line)
        if date_match:
            record_date = date_match.group(1)
            continue

        # 只处理已有 ID 的记录
        id_match = re.match(r"^(\d+)\s*#", line)
        if id_match:
            song_id = int(id_match.group(1))
            try:
                add_song_record(song_id, record_date)
                added_records += 1
            except Exception as e:
                errors.append(f"添加记录失败 - 歌曲ID {song_id} ({e})")
            continue

    if errors:
        msg = "更新歌单记录时出现错误。"
        logger.error("更新歌单记录时出现错误: %s", "; ".join(errors))
    else:
        msg = "更新成功！"

    # 仅撤回本次回复的机器人歌单草稿；保留未匹配项和提交失败的内容以便修正。
    if not errors and added_records and event.reply:
        draft = event.reply.message.extract_plain_text().strip()
        sender_id = getattr(event.reply.sender, 'user_id', None)
        draft_lines = [line.strip() for line in draft.splitlines() if line.strip()]
        if (
            str(sender_id) == str(bot.self_id)
            and draft_lines
            and re.fullmatch(r'\[\d{4}-\d{2}-\d{2}\]', draft_lines[0])
            and len(draft_lines) > 1
            and all(re.match(r'^\d+\s*#', line) for line in draft_lines[1:])
        ):
            try:
                await bot.delete_msg(message_id=event.reply.message_id)
            except Exception as exc:
                logger.warning('歌单已提交，草稿撤回失败 message_id=%s error=%s',
                               event.reply.message_id, type(exc).__name__)
    await cmd_update_list.finish(msg)
