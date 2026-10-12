import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from src.common.chat_summary_payload import compact_payload, restore_evidence
from src.common.chat_summary import prepare_messages, _complete


class PayloadTests(unittest.TestCase):
    def test_compaction_preserves_text_and_restores_current_evidence(self):
        payload = {
            'period': '10-10 12:00—13:00',
            'context': {'terms': [
                {'term': '三理', 'meaning': '三理Mit3uri，25期生'},
                {'term': '理理', 'meaning': '三理Mit3uri，25期生'},
            ], 'bot_commands': [
                {'usage': '来点 <名字>', 'meaning': '随机抽取'},
                {'usage': '查直播 [日期]', 'meaning': '查询直播'},
            ]},
            'people': {'U001': {'name': 'Pecco', 'aliases': ['pc', '旧昵称'], 'confirmed_aliases': ['pc']}},
            'messages': [{'id': '1234567890', 'time': '12:30', 'user': 'U001',
                          'name': 'Pecco', 'text': '来点功能怎么报错了？', 'nearby_unread_image': True}],
            'previous_summary': {'topics': [{'title': '上轮', 'summary': '历史话题', 'message_ids': ['old']}], 'terms_to_check': []},
        }
        original = json.dumps(payload, ensure_ascii=False)
        packed, mapping = compact_payload(payload)
        self.assertEqual(json.dumps(payload, ensure_ascii=False), original)
        self.assertEqual(next(iter(packed)), 'context')
        self.assertEqual(packed['context']['terms'], [['三理/理理', '三理Mit3uri，25期生']])
        self.assertEqual(packed['context']['bot_commands'], [['来点 <名字>', '随机抽取']])
        self.assertEqual(packed['messages'][0][:4], ['m1', '12:30', 'U001', '来点功能怎么报错了？'])
        self.assertTrue(packed['messages'][0][4]['nearby_unread_image'])
        self.assertNotIn('name', packed['messages'][0][4])
        self.assertNotIn('message_ids', packed['previous_summary']['topics'][0])
        result = {'topics': [{'message_ids': ['m1', 'unknown']}], 'terms_to_check': []}
        restore_evidence(result, mapping)
        self.assertEqual(result['topics'][0]['message_ids'], ['1234567890', 'unknown'])
        self.assertLess(len(json.dumps(packed, ensure_ascii=False, separators=(',', ':'))), len(original))

    def test_merging_and_review_share_the_same_id_translation(self):
        payload = {'partial_summaries': [
            {'topics': [{'message_ids': ['1234567890', '876543210']}],
             'terms_to_check': [{'message_ids': ['876543210']}]},
        ]}
        packed, mapping = compact_payload(payload)
        self.assertEqual(packed['partial_summaries'][0]['topics'][0]['message_ids'], ['m1', 'm2'])
        self.assertEqual(packed['partial_summaries'][0]['terms_to_check'][0]['message_ids'], ['m2'])
        restore_evidence({'topics': [{'message_ids': ['m2']}], 'terms_to_check': []}, mapping)
        self.assertEqual(mapping['m2'], '876543210')

    def test_media_is_folded_but_caption_and_human_bot_discussion_remain(self):
        texts = ['[reply][image][image]这个图里的数值怎么回事', '[forward][forward]',
                 '/查歌曲 好听', '我刚才用查歌曲查不到了', 'addmeme 标签 这是一段素材']
        rows = [{'message_id': i, 'user_id': 12345, 'nickname': '甲', 'sent_at': 100 + i,
                 'segments': json.dumps([{'type': 'text', 'text': t}])}
                for i, t in enumerate(texts, 1)]
        rows.append({'message_id': 6, 'user_id': 99999, 'nickname': 'Libot', 'sent_at': 106,
                     'segments': json.dumps([{'type': 'text', 'text': '无效机器人回复' * 100}])})
        messages, _ = prepare_messages(rows, {}, '99999')
        self.assertEqual([m['id'] for m in messages], ['1', '3', '4', '5'])
        self.assertEqual(messages[0]['text'], '[图片]这个图里的数值怎么回事')
        self.assertTrue(messages[0]['nearby_unread_image'])


class RequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_receives_compact_json_and_returns_original_evidence(self):
        client = AsyncMock()
        client.post.return_value = httpx.Response(200, json={
            'model': 'qwen3.8-flash',
            'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({
                'topics': [{'title': '讨论', 'summary': '正常正文', 'message_ids': ['m1']}],
                'terms_to_check': [],
            })}}], 'usage': {'prompt_tokens': 100, 'completion_tokens': 20},
        })
        client.__aenter__.return_value = client
        payload = {'context': {'terms': []}, 'people': {},
                   'messages': [{'id': '9876543210', 'time': '12:00', 'user': 'U001', 'text': '完整正文'}]}
        with patch('src.common.chat_summary.api_config', return_value=('https://example.com/v1/chat/completions', 'test', 'qwen3.8-flash')), patch(
            'src.common.chat_summary.httpx.AsyncClient', return_value=client
        ), patch.dict('os.environ', {'AI_MAX_OUTPUT_TOKENS': '4000'}):
            result, usage = await _complete(payload)
        sent = client.post.call_args.kwargs['json']
        packed = json.loads(sent['messages'][1]['content'])
        self.assertEqual(packed['messages'], [['m1', '12:00', 'U001', '完整正文']])
        self.assertEqual(result['topics'][0]['message_ids'], ['9876543210'])
        self.assertEqual(usage['model'], 'qwen3.8-flash')
        self.assertEqual(sent['max_tokens'], 4000)
        self.assertFalse(sent['enable_thinking'])
