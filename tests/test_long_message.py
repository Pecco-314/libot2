import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from nonebot.adapters.onebot.v11 import Message, MessageSegment
from src.common.long_message import split_text
from src.plugins.utils import send_foldable_message, send_forward_message
from src.plugins.message_delivery import install_message_delivery


class LongMessageTests(unittest.IsolatedAsyncioTestCase):
    def test_lossless_chunks(self):
        for text in ('', 'a\r\nb\n', '字' * 5000, '短\n' + '长' * 5000 + '\n尾'):
            chunks = split_text(text)
            self.assertEqual(''.join(chunks), text)
            self.assertTrue(all(0 < len(c) <= 1500 for c in chunks))

    async def test_threshold_and_lines(self):
        bot = SimpleNamespace(self_id='1', send=AsyncMock())
        with patch('src.plugins.utils.send_forward_message', new_callable=AsyncMock) as forward:
            await send_foldable_message(bot, object(), '字' * 300)
            bot.send.assert_awaited_once()
            await send_foldable_message(bot, object(), '字' * 301)
            await send_foldable_message(bot, object(), '\n'.join(['a'] * 11))
            await send_foldable_message(bot, object(), '字' * 31, threshold=30)
            await send_foldable_message(bot, object(), '短', force=True)
            self.assertEqual(forward.await_count, 4)

    async def test_segments_prefix_and_batching(self):
        bot = SimpleNamespace(self_id='1', send=AsyncMock())
        prefix = Message(MessageSegment.at(123))
        source = Message([MessageSegment.text('字' * 76000), MessageSegment.image('https://example.com/a.png')])
        with patch('src.plugins.utils.send_forward_message', new_callable=AsyncMock) as forward:
            await send_foldable_message(bot, object(), source, prefix=prefix)
            self.assertEqual(forward.await_count, 2)
            nodes = [node for call in forward.call_args_list for node in call.args[2]]
            restored = Message()
            for node in nodes:
                restored += node['data']['content']
            self.assertEqual(restored.extract_plain_text(), '字' * 76000)
            self.assertEqual(restored[0], MessageSegment.at(123))
            self.assertEqual(restored[-1].type, 'image')

    async def test_private_transport(self):
        bot = SimpleNamespace(call_api=AsyncMock())
        event = SimpleNamespace(get_user_id=lambda: '42')
        await send_forward_message(bot, event, [])
        bot.call_api.assert_awaited_once_with('send_private_forward_msg', user_id=42, messages=[])

    async def test_command_default_delivery(self):
        direct = AsyncMock(return_value={'message_id': 11})
        class Command:
            send = direct
        install_message_delivery(Command)
        self.assertEqual(await Command.send('短'), {'message_id': 11})
        with patch('src.plugins.message_delivery.current_bot') as bot_context, patch('src.plugins.message_delivery.current_event') as event_context, patch('src.plugins.message_delivery.send_foldable_message', new_callable=AsyncMock) as folded:
            folded.return_value = {'message_id': 12}
            self.assertEqual(await Command.send('长' * 301), {'message_id': 12})
            folded.assert_awaited_once()
            await Command.send('长' * 301, reply_message=True)
            self.assertEqual(direct.await_count, 2)

    async def test_multiple_text_segments_keep_node_bound(self):
        bot = SimpleNamespace(self_id='1', send=AsyncMock())
        source = Message([MessageSegment.text('a' * 1000), MessageSegment.at(12), MessageSegment.text('b' * 1000)])
        with patch('src.plugins.utils.send_forward_message', new_callable=AsyncMock) as forward:
            await send_foldable_message(bot, object(), source)
            nodes = forward.call_args.args[2]
            self.assertTrue(all(len(n['data']['content'].extract_plain_text()) <= 1500 for n in nodes))
