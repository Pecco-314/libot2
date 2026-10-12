import asyncio
import json
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from src.common.chat_summary import _complete, _qwen_complete, summarize
from src.common.chat_summary_codex import CodexClient, CodexUnavailable, process_config, get_client
from src.common import chat_summary_service as service
from src.db import chat_summary as db
from tests.test_chat_summary_batches import Fixtures, rows, result


def payload():
    return {'context': {'terms': []}, 'people': {}, 'messages': [
        {'id': '123456', 'time': '12:00', 'user': 'U001', 'text': '一段讨论'}]}


class CodexConfigTests(unittest.TestCase):
    def test_server_route_does_not_inherit_pc_proxy(self):
        with patch.dict(os.environ, {'AI_CODEX_USE_PC_BRIDGE': 'false', 'AI_CODEX_PROXY_URL': '',
                                    'AI_CODEX_CA_BUNDLE': '', 'HTTP_PROXY': 'http://127.0.0.1:17890',
                                    'WSS_PROXY': 'http://127.0.0.1:17890', 'SSL_CERT_FILE': '/pc/ca'}):
            args, env, bridge, proxy = process_config()
        self.assertFalse(bridge)
        self.assertFalse(proxy)
        self.assertNotIn('HTTP_PROXY', env)
        self.assertNotIn('WSS_PROXY', env)
        self.assertNotIn('SSL_CERT_FILE', env)
        self.assertIn('model_provider="openai"', args)
        self.assertIn('chatgpt_base_url="https://chatgpt.com/backend-api/"', args)
        self.assertIn('features.shell_tool=false', args)
        self.assertIn('features.hooks=false', args)
        self.assertIn('features.plugins=false', args)
        self.assertIn('project_doc_max_bytes=0', args)
        self.assertIn('service_tier="fast"', args)
        self.assertIn('features.fast_mode=true', args)
        self.assertNotEqual(args[0], '/home/pecco/.local/bin/codex')

    def test_standard_tier_disables_fast_and_rejects_unknown_tier(self):
        with patch.dict(os.environ, {'AI_CODEX_SERVICE_TIER': 'default'}):
            args, *_ = process_config()
        self.assertIn('features.fast_mode=false', args)
        self.assertIn('service_tier="default"', args)
        with patch.dict(os.environ, {'AI_CODEX_SERVICE_TIER': 'unknown'}):
            with self.assertRaises(ValueError):
                process_config()

    def test_proxy_must_be_local_and_pc_requires_explicit_bridge_mode(self):
        for url in ('http://remote.example:7890', 'http://user:pass@127.0.0.1:7890',
                    'http://127.0.0.1:17890', 'http://127.0.0.1:17891'):
            with self.subTest(url=url), patch.dict(os.environ, {
                'AI_CODEX_USE_PC_BRIDGE': 'false', 'AI_CODEX_PROXY_URL': url,
            }), self.assertRaises(ValueError):
                process_config()
        with patch.dict(os.environ, {'AI_CODEX_USE_PC_BRIDGE': 'false',
                                    'AI_CODEX_PROXY_URL': 'http://127.0.0.1:17892'}):
            _, env, _, proxy = process_config()
        self.assertEqual(proxy, 'http://127.0.0.1:17892')
        self.assertEqual(env['WSS_PROXY'], proxy)
        self.assertEqual(env['HTTPS_PROXY'], proxy)

    def test_pc_bridge_sets_both_routes_explicitly_without_changing_user_config(self):
        with patch.dict(os.environ, {'AI_CODEX_USE_PC_BRIDGE': 'true', 'AI_CODEX_PROXY_URL': '',
                                    'AI_CODEX_CA_BUNDLE': '', 'API_KEY': 'do-not-pass',
                                    'AI_CODEX_TEST_SECRET': 'do-not-pass'}):
            args, env, bridge, proxy = process_config()
        self.assertTrue(bridge)
        self.assertEqual(proxy, 'http://127.0.0.1:17890')
        self.assertEqual(env['HTTPS_PROXY'], proxy)
        self.assertIn('model_provider="libot-pc-bridge"', args)
        self.assertIn('model_providers.libot-pc-bridge.base_url="http://127.0.0.1:17891/backend-api/codex"', args)
        self.assertIn('chatgpt_base_url="http://127.0.0.1:17891/backend-api/"', args)
        self.assertNotIn('API_KEY', env)
        self.assertNotIn('AI_CODEX_TEST_SECRET', env)
        self.assertIn('SSL_CERT_FILE', env)


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    def http_client(self, status=401):
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.get.return_value = httpx.Response(status)
        return client

    async def test_pc_probes_both_upstreams_without_authentication_or_model_call(self):
        mock = self.http_client()
        with patch('src.common.chat_summary_codex.httpx.AsyncClient', return_value=mock) as factory:
            await CodexClient().check_network({}, True, 'http://127.0.0.1:17890')
        self.assertEqual(mock.get.await_count, 2)
        urls = [call.args[0] for call in mock.get.await_args_list]
        self.assertTrue(any(url.startswith('http://127.0.0.1:17891/') for url in urls))
        self.assertTrue(any(url.startswith('https://chatgpt.com/') for url in urls))
        self.assertEqual({call.kwargs['proxy'] for call in factory.call_args_list},
                         {None, 'http://127.0.0.1:17890'})
        for call in factory.call_args_list:
            self.assertFalse(call.kwargs['trust_env'])
            self.assertEqual(call.kwargs['timeout'], 10)
            self.assertNotIn('headers', call.kwargs)
            self.assertTrue(call.kwargs['verify'])

    async def test_blocked_or_failed_gateway_is_not_considered_online(self):
        for code in (302, 403, 407, 404, 429, 500, 502, 503):
            with self.subTest(code=code), patch(
                    'src.common.chat_summary_codex.httpx.AsyncClient', return_value=self.http_client(code)):
                with self.assertRaises(CodexUnavailable):
                    await CodexClient().check_network({}, True, 'http://127.0.0.1:17890')

    async def test_connection_failure_and_timeout_are_unavailable(self):
        for error in (httpx.ConnectError('offline'), httpx.ReadTimeout('timeout'), TimeoutError()):
            mock = self.http_client()
            mock.get.side_effect = error
            with self.subTest(error=type(error).__name__), patch(
                    'src.common.chat_summary_codex.httpx.AsyncClient', return_value=mock):
                with self.assertRaises(CodexUnavailable):
                    await CodexClient().check_network({}, True, 'http://127.0.0.1:17890')

    async def test_existing_long_lived_process_is_still_probed(self):
        client = CodexClient()
        client.process = SimpleNamespace(returncode=None)
        client.reader = SimpleNamespace(done=lambda: False)
        with patch.object(client, 'check_network', AsyncMock()) as check, patch(
                'src.common.chat_summary_codex.asyncio.create_subprocess_exec', AsyncMock()) as start:
            await client.ensure_started()
            await client.ensure_started()
        self.assertEqual(check.await_count, 2)
        start.assert_not_awaited()

    async def test_failed_preflight_sets_cooldown_without_starting_a_model(self):
        client = CodexClient()
        with patch.object(client, 'check_network', AsyncMock(side_effect=CodexUnavailable('offline'))), patch(
                'src.common.chat_summary_codex.asyncio.create_subprocess_exec', AsyncMock()) as start:
            with self.assertRaises(CodexUnavailable):
                await client.complete('test', {})
        self.assertFalse(client.available_for_retry())
        start.assert_not_awaited()


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_codex_success_restores_evidence_and_never_calls_flash(self):
        answer = {'topics': [{'title': '标题', 'summary': '摘要', 'message_ids': ['m1']}], 'terms_to_check': []}
        primary = AsyncMock(return_value=(answer, {'backend': 'codex'}))
        with patch.dict(os.environ, {'AI_BACKEND': 'codex'}), patch(
            'src.common.chat_summary_codex.complete', primary), patch(
            'src.common.chat_summary._qwen_complete', AsyncMock()) as fallback:
            value, usage = await _complete(payload())
        self.assertEqual(value['topics'][0]['message_ids'], ['123456'])
        self.assertEqual(usage['backend'], 'codex')
        fallback.assert_not_awaited()
        sent = primary.call_args.args[1]
        self.assertEqual(sent['messages'][0][0], 'm1')

    async def test_codex_failure_falls_back_and_primary_only_does_not(self):
        with patch.dict(os.environ, {'AI_BACKEND': 'codex'}), patch(
            'src.common.chat_summary_codex.complete', AsyncMock(side_effect=CodexUnavailable('offline'))), patch(
            'src.common.chat_summary._qwen_complete', AsyncMock(return_value=(result(), {}))) as fallback:
            await _complete(payload())
            self.assertTrue(fallback.call_args.kwargs['fallback'])
            fallback.reset_mock()
            with self.assertRaises(CodexUnavailable):
                await _complete(payload(), primary_only=True)
            fallback.assert_not_awaited()

    async def test_fallback_selects_flash_even_if_regular_qwen_uses_max(self):
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = httpx.Response(200, json={
            'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(result())}}]})
        with patch.dict(os.environ, {'AI_FALLBACK_MODEL': 'qwen3.8-flash', 'AI_MAX_OUTPUT_TOKENS': '4000'}), patch(
            'src.common.chat_summary.api_config', return_value=('https://example.com', 'fake', 'qwen3.8-max')), patch(
            'src.common.chat_summary.httpx.AsyncClient', return_value=client):
            _, usage = await _qwen_complete({}, {}, fallback=True)
        self.assertEqual(client.post.call_args.kwargs['json']['model'], 'qwen3.8-flash')
        self.assertTrue(usage['fallback_from_codex'])

    async def test_cooldown_queries_do_not_postpone_recovery(self):
        client = CodexClient()
        client.retry_after = time.monotonic() + 100
        before = client.retry_after
        with patch.object(client, 'close', AsyncMock()) as close:
            for _ in range(3):
                with self.assertRaises(CodexUnavailable):
                    await client.complete('prompt', {})
        self.assertEqual(client.retry_after, before)
        close.assert_not_awaited()

    async def test_client_is_reused_by_the_same_event_loop(self):
        self.assertIs(get_client(), get_client())
        self.assertIsNotNone(asyncio.get_running_loop())

    async def test_fallback_summary_is_marked_for_original_message_reanalysis(self):
        value = result('123456')
        value['terms_to_check'] = []
        with patch('src.common.chat_summary._complete', AsyncMock(return_value=(value, {'fallback_from_codex': True}))):
            answer = await summarize(payload()['messages'], {}, '测试', review=False, context={'terms': []})
        self.assertTrue(answer['needs_primary_reanalysis'])


class RecoveryTests(Fixtures, unittest.IsolatedAsyncioTestCase):
    def complete_fallback(self):
        records = rows()
        self.insert(records)
        bid = db.stage_batch(1, records, self.path)
        claim = db.claim_batch(bid, db_path=self.path)
        value = result()
        value['needs_primary_reanalysis'] = True
        db.finish_batch(bid, value, db.revision(self.path), claim['token'], self.path)
        return bid, records

    async def test_retention_is_bounded_and_claim_is_exclusive(self):
        bid, records = self.complete_fallback()
        self.assertEqual(db.recovery_candidates(self.path), [{'batch_id': bid, 'group_id': 1}])
        first = db.claim_recovery(bid, self.path)
        self.assertEqual(first['rows'], records)
        self.assertIsNone(db.claim_recovery(bid, self.path))
        self.assertEqual(first['expires_at'], records[0]['sent_at'] + db.RAW_TTL)
        db.release_recovery(bid, first['claim_token'], self.path)
        self.assertIsNone(db.claim_recovery(bid, self.path))
        with db.connection(self.path) as conn:
            retry = conn.execute('SELECT retry_after FROM chat_summary_recovery WHERE batch_id=?', (bid,)).fetchone()[0]
        self.assertGreaterEqual(retry, int(time.time()) + 299)
        self.assertLessEqual(retry, int(time.time()) + 300)
        self.assertEqual(db.recovery_candidates(self.path, now=first['expires_at']), [])
        self.assertTrue(json.loads(db.get_batch(bid, self.path)['result'])['needs_primary_reanalysis'])

    async def test_success_keeps_revision_and_does_not_move_cursor(self):
        bid, _ = self.complete_fallback()
        before = db.get_cycle(1, self.path)
        value = result()
        with patch.object(service, 'analyze_rows', AsyncMock(return_value=(value, db.revision(self.path)))) as analyze:
            self.assertTrue(await service.recover_primary_summary(bid, db_path=self.path, cache_path=self.cache))
        self.assertTrue(analyze.call_args.kwargs['primary_only'])
        self.assertNotIn('previous', analyze.call_args.kwargs)
        after = db.get_cycle(1, self.path)
        self.assertEqual((before['cursor_ts'], before['cursor_mid']), (after['cursor_ts'], after['cursor_mid']))
        self.assertEqual(db.recovery_candidates(self.path), [])
        self.assertNotIn('needs_primary_reanalysis', json.loads(after['result']))
        with db.connection(self.path) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM chat_summary_revision').fetchone()[0], 1)

    async def test_failure_preserves_fallback_and_schedules_retry(self):
        bid, _ = self.complete_fallback()
        before = db.get_batch(bid, self.path)['result']
        with patch.object(service, 'analyze_rows', AsyncMock(side_effect=CodexUnavailable('offline'))):
            with self.assertRaises(CodexUnavailable):
                await service.recover_primary_summary(bid, db_path=self.path, cache_path=self.cache)
        self.assertEqual(db.get_batch(bid, self.path)['result'], before)
        self.assertEqual(db.recovery_candidates(self.path), [])

    async def test_concurrent_manual_reanalysis_is_not_overwritten(self):
        bid, _ = self.complete_fallback()
        async def concurrent(*args, **kwargs):
            manual = result()
            manual['topics'][0]['summary'] = '人工重分析之后的结果'
            db.save_reanalysis(bid, manual, db.revision(self.path), self.path)
            return result(), db.revision(self.path)
        with patch.object(service, 'analyze_rows', concurrent):
            self.assertFalse(await service.recover_primary_summary(bid, db_path=self.path, cache_path=self.cache))
        stored = json.loads(db.get_batch(bid, self.path)['result'])
        self.assertEqual(stored['topics'][0]['summary'], '人工重分析之后的结果')
        self.assertEqual(db.recovery_candidates(self.path), [])

    async def test_global_concurrency_includes_recovery(self):
        bid, _ = self.complete_fallback()
        self.assertIsNotNone(db.claim_recovery(bid, self.path))
        self.assertIsNotNone(db.claim_cycle(2, self.path))
        self.assertIsNone(db.claim_cycle(3, self.path))
