"""歌曲资料和演唱歌单录入。"""

from __future__ import annotations

from src.common.time_args import normalize_date, is_date_arg

import asyncio
import re
import os

from nonebot.adapters.onebot.v11 import Bot, Event, GroupMessageEvent, Message
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER

from src.db.song_list import add_new_song, submit_song_list, search_songs_by_title
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

    # 行内及整行 # 注释不参与日期解析和歌曲匹配。
    lines = [content for line in raw_text.splitlines()
             if (content := line.split('#', 1)[0].strip())]
    if not lines:
        await cmd_generate_list.finish(
            "请提供歌单文本，每行一首。\n"
            "例如：\n/生成歌单 2025-03-01\n南风过隙\nLove 2000"
        )

    record_date = normalize_date("今天")
    first_line = lines[0]
    is_date_line = is_date_arg(first_line)
    if is_date_line:
        try:
            record_date = normalize_date(first_line)
        except ValueError as exc:
            await cmd_generate_list.finish(str(exc))
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

        # 候选分层排序，低置信度同名或拼写纠错需明确歌手后再提交。
        search_res = search_songs_by_title(search_title, singer=search_singer, limit=3)
        if search_res and search_res[0].get('needs_confirmation'):
            candidates = '；'.join(f"{s['title']} | {s['original_singer']}" for s in search_res)
            result_lines.append(f'? # {line} (请确认歌名或歌手：{candidates})')
        elif search_res:
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
    # 写库权限以提交者为准，与被回复消息的作者无关；API失败时拒绝提交。
    trusted = event.get_user_id() == os.environ.get('MANAGER_QQ', '').strip() or await SUPERUSER(bot, event)
    if not trusted:
        try:
            member = await bot.get_group_member_info(group_id=event.group_id,
                                                     user_id=int(event.get_user_id()), no_cache=True)
        except Exception:
            await cmd_update_list.finish('无法确认管理员权限，请稍后重试')
            return
        if member.get('role') not in {'owner', 'admin'}:
            await cmd_update_list.finish('权限不足：提交歌单仅群管理员或超级管理员可用')
            return
    if any(segment.type != 'text' for segment in arg):
        await cmd_update_list.finish('歌单必须为纯文本')
        return
    text = arg.extract_plain_text().strip()

    if event.reply:
        if text or any(segment.type != 'text' for segment in event.reply.message):
            await cmd_update_list.finish('回复提交时不要附加内容，且被回复歌单必须为纯文本')
            return
        reply_text = event.reply.message.extract_plain_text().strip()
        text = reply_text

    if not text:
        await cmd_update_list.finish("请回复或提供歌单文本。")

    try:
        await asyncio.to_thread(submit_song_list, text)
    except ValueError as exc:
        await cmd_update_list.finish(f'提交失败：{exc}，未写入任何记录')
        return
    except Exception:
        logger.exception('提交歌单事务失败')
        await cmd_update_list.finish('提交失败，未写入任何记录，请稍后重试')
        return
    msg = '更新成功！'

    # 仅撤回本次回复的机器人歌单草稿；保留未匹配项和提交失败的内容以便修正。
    if event.reply:
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
