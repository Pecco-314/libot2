"""命令回复的默认长消息策略；后台通知保持各自的发送流程。"""
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.matcher import current_bot, current_event

from src.plugins.utils import send_foldable_message
from src.common.long_message import needs_forward


def install_message_delivery(matcher):
    original_send = matcher.send

    async def send(cls, message, **kwargs):
        # 带显式发送选项或模板的调用交由框架处理，保留适配器语义。
        if not kwargs and isinstance(message, (str, Message, MessageSegment)):
            content = Message(MessageSegment.text(message)) if isinstance(message, str) else Message(message)
            text = content.extract_plain_text()
            if needs_forward(text):
                return await send_foldable_message(current_bot.get(), current_event.get(), content)
        return await original_send(message, **kwargs)

    matcher.send = classmethod(send)
