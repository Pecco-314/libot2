"""同名查询依次复用单用户命令，收集回复后扁平合并转发。"""
import logging

from nonebot.adapters.onebot.v11 import Message, MessageSegment
from src.common.long_message import split_text
from src.plugins.utils import send_forward_message

logger = logging.getLogger('libot.commands')


class CollectedFinish(Exception):
    pass


class ReplyCollector:
    def __init__(self, bot):
        self.bot = bot
        self.self_id = bot.self_id
        self.messages = []

    async def send(self, *args, **kwargs):
        # Matcher.send(message) 与 Bot.send(event, message) 共用收集器。
        message = kwargs.get('message', args[-1] if args else None)
        if message is not None:
            self.messages.append(message)
        return {'message_id': 0}

    async def finish(self, message=None, **kwargs):
        if message is not None:
            self.messages.append(message)
        raise CollectedFinish()

    async def call_api(self, api, **kwargs):
        if api in {'send_group_forward_msg', 'send_private_forward_msg'}:
            for node in kwargs['messages']:
                self.messages.append(node['data']['content'])
            return {'message_id': 0}
        if api in {'send_group_msg', 'send_private_msg'}:
            return await self.send(kwargs['message'])
        return await self.bot.call_api(api, **kwargs)


async def run_user_batch(bot, event, query, targets, handler, make_arg=str):
    """targets为UID或已展开的主播组合；逐项错误不会中止其他项。"""
    def node(content):
        return {'type': 'node', 'data': {'name': 'Libot', 'uin': bot.self_id, 'content': content}}

    nodes = [node(MessageSegment.text(f'名称“{query}”对应多个用户，以下分别查询：'))]
    for target in targets:
        collector = ReplyCollector(bot)
        nodes.append(node(MessageSegment.text(f'查询对象：{target}')))
        try:
            await handler(collector, collector, event, arg=Message(make_arg(target)))
        except CollectedFinish:
            pass
        except Exception:
            logger.exception('同名批量查询失败 target=%s', target)
            collector.messages.append('该用户查询失败，请稍后使用UID重试')
        if not collector.messages:
            collector.messages.append('暂无记录')
        for message in collector.messages:
            # 文本无损分片，图片保持原消息段；不嵌套已有合并转发。
            msg = Message(message) if not isinstance(message, str) else Message(MessageSegment.text(message))
            for segment in msg:
                if segment.type == 'text':
                    nodes.extend(node(MessageSegment.text(text)) for text in split_text(segment.data['text']))
                else:
                    nodes.append(node(segment))
    for offset in range(0, len(nodes), 50):
        await send_forward_message(bot, event, nodes[offset:offset+50])
