"""本地表情制作命令。"""

import asyncio
from urllib.parse import urlsplit
from nonebot.adapters.onebot.v11 import Event, MessageSegment
from nonebot.matcher import Matcher
from nonebot.params import CommandArg

from src.common.meme_image import MemeImageError, download_meme_image, extract_reply_image_url, resolve_meme_image_path
from src.plugins.command_registry import good_news_cmd, bad_news_cmd, sold_out_cmd
from src.render.meme_templates import render_news, render_sold_out

_SLOTS = asyncio.Semaphore(2)


async def _news(matcher, arg, good):
    try:
        async with _SLOTS:
            result = await asyncio.to_thread(render_news, arg.extract_plain_text(), good=good)
    except ValueError as exc:
        await matcher.finish(str(exc))
    await matcher.finish(MessageSegment.image(result))


@good_news_cmd.handle()
async def good_news(matcher: Matcher, arg=CommandArg()):
    await _news(matcher, arg, True)


@bad_news_cmd.handle()
async def bad_news(matcher: Matcher, arg=CommandArg()):
    await _news(matcher, arg, False)


@sold_out_cmd.handle()
async def sold_out(matcher: Matcher, event: Event, arg=CommandArg()):
    url = extract_reply_image_url(event)
    if not url:
        for segment in arg:
            if segment.type == 'image':
                for key in ('url', 'file'):
                    value = str(segment.data.get(key) or '')
                    if urlsplit(value).scheme in {'http', 'https'}:
                        url = value
                        break
            if url:
                break
    if not url:
        await matcher.finish('请回复一张图片发送“卖掉了”，或在命令后附图')
    try:
        async with _SLOTS:
            saved = await download_meme_image(url)
            path = resolve_meme_image_path(saved)
            if path is None:
                raise MemeImageError('图片读取失败')
            result = await asyncio.to_thread(render_sold_out, path)
    except MemeImageError as exc:
        await matcher.finish(str(exc))
    await matcher.finish(MessageSegment.image(result))
