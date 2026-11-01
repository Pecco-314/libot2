import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from nonebot.adapters.onebot.v11 import MessageSegment
from src.plugins.user_query_batch import run_user_batch


class UserQueryBatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_separate_queries_flatten_images_and_keep_arguments(self):
        bot = SimpleNamespace(self_id='123', call_api=AsyncMock())
        queries = []

        async def handler(matcher, child_bot, event, arg):
            queries.append(arg.extract_plain_text())
            await child_bot.call_api('send_group_forward_msg', group_id=1, messages=[{
                'type': 'node', 'data': {'content': MessageSegment.image('file:///tmp/test.png')}}])
            await matcher.finish('查询完成')

        with patch('src.plugins.user_query_batch.send_forward_message', new_callable=AsyncMock) as send:
            await run_user_batch(bot, object(), '同名', [11,22], handler, lambda uid: f'{uid} 昨天 10')
        self.assertEqual(queries, ['11 昨天 10', '22 昨天 10'])
        nodes = send.await_args.args[2]
        self.assertIn('对应多个用户', str(nodes[0]['data']['content']))
        self.assertEqual(sum(n['data']['content'].type == 'image' for n in nodes), 2)
        self.assertIn('11', str(nodes[1]['data']['content']))
        self.assertIn('22', str(nodes[4]['data']['content']))
        bot.call_api.assert_not_called()

    async def test_one_failure_does_not_stop_other_users(self):
        bot = SimpleNamespace(self_id='123')
        async def handler(matcher, child_bot, event, arg):
            if str(arg) == '11':
                raise ValueError('test')
            await matcher.finish('暂无牌子')
        with patch('src.plugins.user_query_batch.send_forward_message', new_callable=AsyncMock) as send:
            await run_user_batch(bot, object(), '同名', [11,22], handler)
        text = ''.join(str(n['data']['content']) for n in send.await_args.args[2])
        self.assertIn('查询失败', text)
        self.assertIn('暂无牌子', text)

    async def test_forward_batches_are_bounded(self):
        bot = SimpleNamespace(self_id='123')
        async def handler(matcher, child_bot, event, arg):
            for i in range(30):
                await matcher.send(MessageSegment.image(f'file:///tmp/{i}.png'))
        with patch('src.plugins.user_query_batch.send_forward_message', new_callable=AsyncMock) as send:
            await run_user_batch(bot, object(), '同名', [11,22], handler)
        self.assertEqual([len(c.args[2]) for c in send.await_args_list], [50,13])


if __name__ == '__main__':
    unittest.main()
