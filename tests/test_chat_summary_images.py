import asyncio
import base64
import io
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

from src.common.chat_summary import _complete, prepare_messages
from src.common.chat_summary_codex import CodexClient, CodexUnavailable
from src.common.chat_summary_images import ImageCatalog, prepare_image_catalog
from src.common.chat_summary_payload import compact_payload


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.assets = Path(self.folder.name)
        self.cache = self.assets / 'cache.db'
        self.stamp = int(time.time())
        self.filename = f'{self.stamp}_1_123_0.img'
        Image.new('RGB', (100, 70), 'blue').save(self.assets / self.filename, format='PNG')
        with sqlite3.connect(self.cache) as conn:
            conn.execute('CREATE TABLE message(group_id,message_id,sent_at,segments,recalled_at)')
            conn.execute('INSERT INTO message VALUES(1,123,?,?,NULL)',
                         (self.stamp, json.dumps([{'type': 'image', 'file': self.filename}])))
        self.catalog = ImageCatalog({'i1': self.filename}, assets=self.assets, cache_path=self.cache)

    def tearDown(self):
        self.folder.cleanup()

    def test_pure_images_reach_model_with_reversible_message_evidence(self):
        row = {'group_id': 1, 'message_id': 123, 'user_id': 456, 'nickname': '甲',
               'sent_at': self.stamp,
               'segments': json.dumps([{'type': 'image', 'file': self.filename}])}
        messages, people = prepare_messages([row], {})
        self.assertEqual(messages[0]['text'], '[图片]')
        payload = {'messages': messages, 'people': people}
        original = json.dumps(payload)
        data, sources = prepare_image_catalog(payload)
        packed, identities = compact_payload(data)
        self.assertEqual(sources, {'i1': self.filename})
        self.assertEqual(packed['messages'][0][4]['images'], ['i1'])
        self.assertNotIn(self.filename, json.dumps(packed))
        self.assertEqual(identities, {'m1': '123'})
        self.assertEqual(json.dumps(payload), original)

    def test_reads_only_selected_image_and_returns_native_image_item(self):
        with patch.object(self.catalog, '_image_url', wraps=self.catalog._image_url) as encode:
            self.assertEqual(self.catalog.read_count, 0)
            answer = self.catalog.read({'image_ids': ['i1']})
            encode.assert_called_once_with(self.filename)
        self.assertTrue(answer['success'])
        self.assertEqual(self.catalog.read_count, 1)
        self.assertEqual(self.catalog.call_count, 1)
        url = answer['contentItems'][1]['imageUrl']
        with Image.open(io.BytesIO(base64.b64decode(url.split(',', 1)[1]))) as image:
            self.assertEqual(image.size, (100, 70))
        self.assertFalse(self.catalog.read({'image_ids': ['i1']})['success'])

    def test_recalled_missing_or_untrusted_files_are_not_read(self):
        with sqlite3.connect(self.cache) as conn:
            conn.execute('UPDATE message SET recalled_at=1')
        self.assertFalse(self.catalog.read({'image_ids': ['i1']})['success'])
        for filename in ('../secret.img', '/etc/passwd', 'https://example.com/pic',
                         f'{self.stamp}_1_999_0.img', '1_1_123_0.img'):
            with self.subTest(filename=filename):
                catalog = ImageCatalog({'i1': filename}, assets=self.assets, cache_path=self.cache)
                self.assertFalse(catalog.read({'image_ids': ['i1']})['success'])
        self.assertFalse(self.catalog.read({'image_ids': ['unknown']})['success'])
        self.assertFalse(self.catalog.read({'image_ids': ['i1'] * 5})['success'])

    def test_symlinks_and_message_filename_mismatch_are_rejected(self):
        filename = f'{self.stamp}_1_124_0.img'
        (self.assets / filename).symlink_to(self.assets / self.filename)
        with sqlite3.connect(self.cache) as conn:
            conn.execute('INSERT INTO message VALUES(1,124,?,?,NULL)',
                         (self.stamp, json.dumps([{'type': 'image', 'file': filename}])))
        catalog = ImageCatalog({'i1': filename}, assets=self.assets, cache_path=self.cache)
        self.assertFalse(catalog.read({'image_ids': ['i1']})['success'])
        with sqlite3.connect(self.cache) as conn:
            conn.execute('UPDATE message SET segments=\'[]\',recalled_at=NULL WHERE message_id=123')
        catalog = ImageCatalog({'i1': self.filename}, assets=self.assets, cache_path=self.cache)
        self.assertFalse(catalog.read({'image_ids': ['i1']})['success'])

    def test_total_image_budget_is_bounded(self):
        catalog = ImageCatalog({f'i{i}': self.filename for i in range(1, 15)},
                               assets=self.assets, cache_path=self.cache)
        with patch.object(catalog, '_image_url', return_value='data:image/jpeg;base64,test'):
            for i in range(1, 15):
                catalog.read({'image_ids': [f'i{i}']})
        self.assertEqual(catalog.read_count, 12)
        self.assertEqual(len(catalog.requested), 12)


class CodexImageTests(unittest.IsolatedAsyncioTestCase):
    def answer(self):
        return {'topics': [{'title': '讨论', 'summary': '内容', 'message_ids': ['m1']}],
                'terms_to_check': []}

    async def test_only_codex_receives_private_image_sources(self):
        source = f'{int(time.time())}_1_123_0.img'
        payload = {'messages': [{'id': '123', 'text': '[图片]', 'image_files': [source]}]}
        with patch.dict('os.environ', {'AI_BACKEND': 'codex'}), patch(
                'src.common.chat_summary_codex.complete',
                AsyncMock(return_value=(self.answer(), {}))) as primary:
            result, _ = await _complete(payload)
        self.assertEqual(primary.call_args.kwargs['image_sources'], {'i1': source})
        self.assertNotIn(source, json.dumps(primary.call_args.args))
        self.assertEqual(result['topics'][0]['message_ids'], ['123'])
        with patch.dict('os.environ', {'AI_BACKEND': 'codex'}), patch(
                'src.common.chat_summary_codex.complete', AsyncMock(side_effect=CodexUnavailable())), patch(
                'src.common.chat_summary._qwen_complete',
                AsyncMock(return_value=(self.answer(), {}))) as fallback:
            await _complete(payload)
        self.assertNotIn(source, json.dumps(fallback.call_args.args))
        self.assertNotIn('image_sources', fallback.call_args.kwargs)

    async def test_complete_only_attaches_requested_images_and_preserves_high_fast(self):
        client = CodexClient()
        client.process = SimpleNamespace(returncode=None)
        source = f'{int(time.time())}_1_123_0.img'
        turns = []

        async def request(method, params, **kwargs):
            if method == 'thread/start':
                self.assertEqual(params['model'], 'gpt-6.1-sol')
                self.assertNotIn('dynamicTools', params)
                self.assertEqual(params['serviceTier'], 'fast')
                self.assertEqual(params['approvalPolicy'], 'never')
                return {'thread': {'id': 'thread'}}
            if method == 'turn/start':
                turns.append(params)
                self.assertEqual(params['model'], 'gpt-6.1-sol')
                self.assertEqual(params['effort'], 'high')
                self.assertEqual(params['sandboxPolicy'], {'type': 'readOnly', 'networkAccess': False})
                answer = {**self.answer(), 'image_requests': ['i1'] if len(turns) == 1 else []}
                await client.events['thread'].put({'method': 'item/completed', 'params': {
                    'item': {'type': 'agentMessage', 'text': json.dumps(answer)}}})
                await client.events['thread'].put({'method': 'thread/tokenUsage/updated', 'params': {
                    'tokenUsage': {'last': {'inputTokens': 100, 'outputTokens': 10, 'totalTokens': 110}}}})
                await client.events['thread'].put({'method': 'turn/completed', 'params': {
                    'turn': {'status': 'completed'}}})
            return {}

        with patch.object(client, 'ensure_started', AsyncMock()), patch.object(
                client, 'request', AsyncMock(side_effect=request)), patch(
                'src.common.chat_summary_images.ImageCatalog._image_url',
                return_value='data:image/jpeg;base64,test'), patch.dict(
                'os.environ', {'AI_CODEX_EFFORT': 'high', 'AI_CODEX_SERVICE_TIER': 'fast'}, clear=True):
            answer, usage = await client.complete('仅分析聊天', {}, image_sources={'i1': source})
        self.assertEqual(usage['model'], 'gpt-6.1-sol')
        self.assertEqual(usage['reasoning_effort'], 'high')
        self.assertEqual(usage['service_tier'], 'fast')
        self.assertEqual(usage['images_read'], 1)
        self.assertEqual(usage['turn_count'], 2)
        self.assertEqual(usage['total_tokens'], 220)
        self.assertNotIn('image_requests', answer)
        self.assertFalse(any(item['type'] == 'image' for item in turns[0]['input']))
        self.assertEqual([item['url'] for item in turns[1]['input'] if item['type'] == 'image'],
                         ['data:image/jpeg;base64,test'])
        self.assertFalse(client.events)

    async def test_arbitrary_tool_requests_are_denied(self):
        client = CodexClient()
        stream = asyncio.StreamReader()
        client.process = SimpleNamespace(stdout=stream)
        client._send = AsyncMock()
        future = asyncio.get_running_loop().create_future()
        client.pending[7] = future
        stream.feed_data(b'{"id":1,"method":"item/tool/call","params":{"tool":"read_file"}}\n')
        stream.feed_data(b'{"id":7,"result":{"ok":true}}\n')
        stream.feed_eof()
        await client._read()
        self.assertEqual(await future, {'ok': True})
        self.assertIn('error', client._send.call_args.args[0])

    async def test_no_image_request_keeps_single_turn_and_never_reads_files(self):
        client = CodexClient()
        client.process = SimpleNamespace(returncode=None)

        async def request(method, params, **kwargs):
            return {'thread': {'id': 'thread'}} if method == 'thread/start' else {}

        with patch.object(client, 'ensure_started', AsyncMock()), patch.object(
                client, 'request', AsyncMock(side_effect=request)), patch.object(
                client, '_run_turn', AsyncMock(return_value=(
                    {**self.answer(), 'image_requests': []}, {}))) as turn, patch(
                'src.common.chat_summary_codex.ImageCatalog.read') as read:
            _, usage = await client.complete('test', {}, image_sources={'i1': 'unused.img'})
        self.assertEqual(usage['turn_count'], 1)
        turn.assert_awaited_once()
        read.assert_not_called()

    async def test_image_requests_are_bounded_and_unavailable_images_end_selection(self):
        for unavailable in (False, True):
            with self.subTest(unavailable=unavailable):
                client = CodexClient()
                client.process = SimpleNamespace(returncode=None)
                allow = []

                async def request(method, params, **kwargs):
                    return {'thread': {'id': 'thread'}} if method == 'thread/start' else {}

                async def turn(*args, image_requests, **kwargs):
                    allow.append(image_requests)
                    answer = self.answer()
                    if image_requests:
                        answer['image_requests'] = ['i1']
                    return answer, {}

                response = {'success': not unavailable, 'contentItems': [
                    {'type': 'inputText', 'text': 'unavailable' if unavailable else '已读取图片'}]}
                with patch.object(client, 'ensure_started', AsyncMock()), patch.object(
                        client, 'request', AsyncMock(side_effect=request)), patch.object(
                        client, '_run_turn', AsyncMock(side_effect=turn)), patch(
                        'src.common.chat_summary_codex.ImageCatalog.read', return_value=response) as read:
                    await client.complete('test', {}, image_sources={'i1': 'unused.img'})
                self.assertEqual(allow, [True, False] if unavailable else [True, True, True, False])
                self.assertEqual(read.call_count, 1 if unavailable else 3)
