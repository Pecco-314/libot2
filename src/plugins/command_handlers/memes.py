"""梗库提交、审核入口与每日抽取。"""

from __future__ import annotations

from src.plugins.utils import send_foldable_message

import asyncio

from nonebot.adapters.onebot.v11 import Bot, Event, Message, MessageSegment
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

from src.common.meme_image import (
    MemeImageError,
    download_meme_image,
    extract_reply_image_url,
    extract_reply_text,
    resolve_meme_image_path,
)
from src.db.meme import (
    MemePoolExhausted,
    add_meme_alias,
    add_meme_image_tags,
    add_meme_tags,
    find_meme,
    get_daily_meme,
    get_random_meme,
    is_valid_meme_name,
    list_meme_title_rank,
    remove_meme,
    resolve_daily_meme_name,
)
from src.plugins.command_handlers.songs import _handle_bedtime_song, _handle_random_song
from src.plugins.command_registry import (
    meme_add_cmd,
    meme_alias_cmd,
    meme_image_add_cmd,
    meme_query_cmd,
    meme_rank_cmd,
    meme_remove_cmd,
)
from src.plugins.command_support import _check_modify_permission, logger
from src.plugins.utils import group_manager_required, send_forward_message

_RANDOM_SONG_MEME_NAMES = {"歌曲", "三理歌曲", "随机歌曲"}


@meme_add_cmd.handle()
async def handle_meme_add(matcher: Matcher, event: Event, arg=CommandArg()):
    raw = arg.extract_plain_text().strip()
    replying = getattr(event, "reply", None) is not None
    parts = raw.split() if replying else raw.split(maxsplit=1)
    if not parts:
        await matcher.finish(
            "用法：新增梗 <名字> <内容>，或回复文本后新增梗 <标签1> [标签2 ...]"
        )

    names = parts if replying else [parts[0]]
    content = (
        extract_reply_text(event)
        if replying
        else (parts[1] if len(parts) == 2 else None)
    )
    if content is None:
        await matcher.finish("请提供内容，或回复一条文本消息")
    if not all(is_valid_meme_name(name) for name in names):
        await matcher.finish("名字需为1至32个不含空格的字符")
    if len(content) > 1000:
        await matcher.finish("内容过长，最多1000个字符")

    added = await asyncio.to_thread(
        add_meme_tags,
        names,
        content,
        int(event.get_user_id()),
    )
    if not added:
        await matcher.finish("这条内容已经存在或已在审核中")
    await matcher.finish(
        "已提交至管理员审核" + (f"：{'、'.join(added)}" if len(names) > 1 else "")
    )


@meme_image_add_cmd.handle()
async def handle_meme_image_add(matcher: Matcher, event: Event, arg=CommandArg()):
    names = arg.extract_plain_text().split()
    if not names or not all(is_valid_meme_name(name) for name in names):
        await matcher.finish(
            "用法：回复一张图片后发送 新增图片 <标签1> [标签2 ...]，每个标签为1至32个字符"
        )

    image_url = extract_reply_image_url(event)
    if image_url is None:
        await matcher.finish("请回复一张图片后再使用该命令")

    try:
        image_path = await download_meme_image(image_url)
    except MemeImageError as exc:
        await matcher.finish(str(exc))

    added = await asyncio.to_thread(
        add_meme_image_tags,
        names,
        image_path,
        int(event.get_user_id()),
    )
    if not added:
        await matcher.finish("这张图片已经存在或已在审核中")
    await matcher.finish(
        "已提交至管理员审核" + (f"：{'、'.join(added)}" if len(names) > 1 else "")
    )


@meme_alias_cmd.handle()
@group_manager_required
async def handle_meme_alias(matcher: Matcher, event: Event, arg=CommandArg()):
    names = arg.extract_plain_text().strip().split()
    if len(names) != 2:
        await matcher.finish("用法：新增别名 <名字1> <名字2>")
    if not all(is_valid_meme_name(name) for name in names):
        await matcher.finish("名字需为1至32个不含空格的字符")

    result = await asyncio.to_thread(add_meme_alias, names[0], names[1])
    if not result["changed"]:
        await matcher.finish(f"{names[0]}和{names[1]}已经属于同一组")
    if result["merged"]:
        await matcher.finish(f"已合并{names[0]}和{names[1]}的内容与别名")
    await matcher.finish(f"已将{names[0]}和{names[1]}设为同一组名字")


async def _handle_meme_query(
    matcher: Matcher,
    bot: Bot,
    event: Event,
    name: str,
):
    if not name:
        await matcher.finish("用法：来点 <名字>")
    if name == "晚安曲":
        await _handle_bedtime_song(matcher)
        return
    if name in _RANDOM_SONG_MEME_NAMES:
        await _handle_random_song(matcher, "")
        return
    if not is_valid_meme_name(name):
        await matcher.finish("名字需为1至32个不含空格的字符")

    daily_partner_name = await asyncio.to_thread(resolve_daily_meme_name, name)
    if daily_partner_name is not None:
        try:
            record = await asyncio.to_thread(
                get_daily_meme,
                daily_partner_name,
                int(event.get_user_id()),
            )
        except MemePoolExhausted:
            await matcher.finish("抽完了")
    else:
        record = await asyncio.to_thread(get_random_meme, name)
    if record is None:
        await matcher.finish(f"还没有{name}的内容")

    prefix = Message()
    if daily_partner_name is not None:
        prefix = Message(
            [
                MessageSegment.at(int(event.get_user_id())),
                MessageSegment.text(f" 你今天的{daily_partner_name}是：\n"),
            ]
        )

    if record.get("entry_type") == "image":
        image_path = resolve_meme_image_path(str(record["content"]))
        if image_path is None:
            logger.error("梗图片文件不存在: %s", record["content"])
            await matcher.finish("图片文件不存在，请联系管理员")
        await matcher.finish(prefix + MessageSegment.image(file=str(image_path)))

    content = str(record["content"])
    try:
        await send_foldable_message(bot, event, content, threshold=30, prefix=prefix)
    except Exception as exc:
        logger.error("发送梗内容合并转发失败: %s", exc)
        await matcher.finish("内容发送失败，请稍后再试")


@meme_query_cmd.handle()
async def handle_meme_query(
    matcher: Matcher,
    bot: Bot,
    event: Event,
    arg=CommandArg(),
):
    await _handle_meme_query(
        matcher,
        bot,
        event,
        arg.extract_plain_text().strip(),
    )


@meme_rank_cmd.handle()
async def handle_meme_rank(
    matcher: Matcher,
    bot: Bot,
    event: Event,
    arg=CommandArg(),
):
    raw_limit = arg.extract_plain_text().strip()
    if raw_limit and not raw_limit.isdigit():
        await matcher.finish("用法：memerank [数量]")
    limit = int(raw_limit) if raw_limit else 10
    if not 1 <= limit <= 100:
        await matcher.finish("数量需为1至100")

    items = await asyncio.to_thread(list_meme_title_rank, limit)
    if not items:
        await matcher.finish("目前还没有已审核的meme")
    content = "meme title排行：\n" + "\n".join(
        f"{index}. {item['name']}（{item['count']}条）"
        for index, item in enumerate(items, 1)
    )
    try:
        await send_foldable_message(bot, event, content, threshold=30)
    except Exception as exc:
        logger.error("发送meme排行合并转发失败: %s", exc)
        await matcher.finish("排行发送失败，请稍后再试")


@meme_remove_cmd.handle()
async def handle_meme_remove(matcher: Matcher, event: Event, arg=CommandArg()):
    parts = arg.extract_plain_text().strip().split(maxsplit=1)
    if len(parts) < 2:
        await matcher.finish("用法：删除梗 <名字> <具体内容>")

    name, content = parts
    if not is_valid_meme_name(name):
        await matcher.finish("名字需为1至32个不含空格的字符")

    record = await asyncio.to_thread(find_meme, name, content)
    if record is None:
        await matcher.finish("没有找到这条内容，请填写完整内容")
    if not await _check_modify_permission(record, event):
        await matcher.finish("权限不足：仅群管理员或该内容的添加者可删除")

    removed = await asyncio.to_thread(remove_meme, int(record["id"]))
    if not removed:
        await matcher.finish("系统错误，未能删除")
    await matcher.finish(f"已删除一条{name}内容")
