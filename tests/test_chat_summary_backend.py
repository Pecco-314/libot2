import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from src.common.chat_summary import _complete, _qwen_complete, summarize
from src.common.chat_summary_codex import CodexClient, CodexUnavailable, process_config, get_client, error_details
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
        response = SimpleNamespace(status_code=status, aread=AsyncMock())
        stream = AsyncMock()
        stream.__aenter__.return_value = response
        client.stream = MagicMock(return_value=stream)
        return client

    async def test_pc_probes_actual_backend_headers_without_authentication_or_model_call(self):
        mock = self.http_client()
        with patch('src.common.chat_summary_codex.httpx.AsyncClient', return_value=mock) as factory:
            await CodexClient().check_network({}, True, 'http://127.0.0.1:17890')
        mock.stream.assert_called_once_with('GET',
            'http://127.0.0.1:17891/backend-api/codex/models?client_version=0.160.1')
        mock.stream.return_value.__aenter__.return_value.aread.assert_not_awaited()
        self.assertIsNone(factory.call_args.kwargs['proxy'])
        for call in factory.call_args_list:
            self.assertFalse(call.kwargs['trust_env'])
            self.assertEqual(call.kwargs['timeout'], 10)
            self.assertNotIn('headers', call.kwargs)
            self.assertTrue(call.kwargs['verify'])

    async def test_direct_backend_uses_its_configured_proxy(self):
        mock = self.http_client()
        with patch('src.common.chat_summary_codex.httpx.AsyncClient', return_value=mock) as factory:
            await CodexClient().check_network({}, False, 'http://127.0.0.1:17892')
        self.assertEqual(factory.call_args.kwargs['proxy'], 'http://127.0.0.1:17892')
        self.assertTrue(mock.stream.call_args.args[1].startswith('https://chatgpt.com/'))

    async def test_blocked_or_failed_gateway_is_not_considered_online(self):
        for code in (302, 403, 407, 404, 429, 500, 502, 503):
            with self.subTest(code=code), patch(
                    'src.common.chat_summary_codex.httpx.AsyncClient', return_value=self.http_client(code)):
                with self.assertRaises(CodexUnavailable) as caught:
                    await CodexClient().check_network({}, True, 'http://127.0.0.1:17890')
                self.assertEqual(caught.exception.http_status, code)
                self.assertEqual(caught.exception.stage, 'preflight')

    async def test_connection_failure_and_timeout_are_unavailable(self):
        for error in (httpx.ConnectError('offline'), httpx.ReadTimeout('timeout'), TimeoutError()):
            mock = self.http_client()
            mock.stream.return_value.__aenter__.side_effect = error
            with self.subTest(error=type(error).__name__), patch(
                    'src.common.chat_summary_codex.httpx.AsyncClient', return_value=mock):
                with self.assertRaises(CodexUnavailable) as caught:
                    await CodexClient().check_network({}, True, 'http://127.0.0.1:17890')
                self.assertEqual(caught.exception.cause, type(error).__name__)

    async def test_existing_long_lived_process_checks_network_without_restart(self):
        client = CodexClient()
        client.process = SimpleNamespace(returncode=None)
        client.reader = SimpleNamespace(done=lambda: False)
        with patch.object(client, 'check_network', AsyncMock()) as check, patch(
                'src.common.chat_summary_codex.asyncio.create_subprocess_exec', AsyncMock()) as start:
            await client.ensure_started()
            await client.ensure_started()
        self.assertEqual(check.await_count, 2)
        start.assert_not_awaited()

    async def test_failed_preflight_prevents_start(self):
        client = CodexClient()
        process = SimpleNamespace(returncode=None)
        with tempfile.TemporaryDirectory() as folder, patch(
                'src.common.chat_summary_codex.ROOT', Path(folder)), patch.object(
                client, 'check_network', AsyncMock(side_effect=CodexUnavailable(stage='preflight'))), patch(
                'src.common.chat_summary_codex.asyncio.create_subprocess_exec', AsyncMock(return_value=process)
                ) as start, patch.object(client, '_read', AsyncMock()), patch.object(
                client, 'request', AsyncMock()) as request, patch.object(client, '_send', AsyncMock()):
            (Path(folder) / 'data').mkdir()
            with self.assertRaises(CodexUnavailable):
                await client.ensure_started()
        self.assertTrue(client.available_for_retry())
        start.assert_not_awaited()
        request.assert_not_awaited()

    async def test_failed_initialization_cleans_up_unready_process(self):
        client = CodexClient()
        with tempfile.TemporaryDirectory() as folder, patch(
                'src.common.chat_summary_codex.ROOT', Path(folder)), patch.object(
                client, 'check_network', AsyncMock()), patch(
                'src.common.chat_summary_codex.asyncio.create_subprocess_exec', AsyncMock()), patch.object(
                client, '_read', AsyncMock()), patch.object(client, 'request', AsyncMock(
                    side_effect=CodexUnavailable(stage='initialize'))), patch.object(client, '_close', AsyncMock()) as close:
            (Path(folder) / 'data').mkdir()
            with self.assertRaises(CodexUnavailable):
                await client.ensure_started()
            await client.reader
        close.assert_awaited_once()


class TurnTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = CodexClient()
        self.client.process = SimpleNamespace(returncode=None)
        self.client.events['thread'] = asyncio.Queue()
        self.client.request = AsyncMock(return_value={'turn': {'id': 'turn'}})

    def event(self, method, **params):
        self.client.events['thread'].put_nowait({'method': method, 'params': params})

    def finish(self):
        self.event('item/completed', item={'type': 'agentMessage', 'text': json.dumps({
            'topics': [], 'terms_to_check': []})})
        self.event('turn/completed', turn={'id': 'turn', 'status': 'completed'})

    async def run_turn(self):
        return await self.client._run_turn('thread', [], effort='high', tier='fast', image_requests=False)

    async def test_retryable_error_fails_fast_and_redacts_remote_text(self):
        self.event('error', willRetry=True, turnId='turn', error={
            'message': 'secret request body', 'additionalDetails': 'Bearer secret',
            'codexErrorInfo': {'httpConnectionFailed': {'httpStatusCode': 500}}})
        self.finish()
        with self.assertLogs('src.common.chat_summary_codex', level='WARNING') as logs:
            with self.assertRaises(CodexUnavailable):
                await self.run_turn()
        text = ' '.join(logs.output)
        self.assertIn('http_status=500', text)
        self.assertNotIn('secret', text)
        self.assertEqual([call.args[0] for call in self.client.request.await_args_list],
                         ['turn/start', 'turn/interrupt'])

    async def test_final_error_preserves_status_and_interrupts_only_its_turn(self):
        self.event('error', willRetry=False, error={
            'message': 'secret', 'codexErrorInfo': {'responseTooManyFailedAttempts': {'httpStatusCode': 503}}})
        with self.assertRaises(CodexUnavailable) as caught, patch.object(self.client, 'close', AsyncMock()) as close:
            await self.run_turn()
        self.assertEqual(caught.exception.http_status, 503)
        self.assertEqual(caught.exception.kind, 'responseTooManyFailedAttempts')
        self.assertNotIn('secret', str(caught.exception))
        self.client.request.assert_awaited_with('turn/interrupt', {'threadId': 'thread', 'turnId': 'turn'}, timeout=5)
        close.assert_not_awaited()

    async def test_failure_in_completed_event_preserves_error(self):
        self.event('turn/completed', turn={'id': 'turn', 'status': 'failed',
                                         'error': {'codexErrorInfo': 'unauthorized'}})
        with self.assertRaises(CodexUnavailable) as caught:
            await self.run_turn()
        self.assertEqual(caught.exception.kind, 'unauthorized')

    async def test_silent_wait_does_not_probe_or_abort(self):
        with patch.object(self.client, 'check_network', AsyncMock(
                side_effect=AssertionError('must not probe before interval'))) as probe:
            task = asyncio.create_task(self.run_turn())
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.finish()
            await task
            probe.assert_not_awaited()

    async def test_idle_network_failure_interrupts_turn(self):
        async def timeout(awaitable, *args):
            awaitable.close()
            raise asyncio.TimeoutError()
        with patch('src.common.chat_summary_codex.asyncio.wait_for', side_effect=timeout), patch.object(
                self.client, 'check_network', AsyncMock(side_effect=CodexUnavailable(stage='preflight'))):
            with self.assertRaises(CodexUnavailable):
                await self.run_turn()
        self.assertEqual(self.client.request.await_args_list[-1].args[0], 'turn/interrupt')

    async def test_stale_turn_error_does_not_abort_current_turn(self):
        self.event('error', turnId='previous', willRetry=False, error={'codexErrorInfo': 'badRequest'})
        self.finish()
        value, _ = await self.run_turn()
        self.assertEqual(value['topics'], [])

    async def test_cancellation_interrupts_own_turn_without_closing_process(self):
        task = asyncio.create_task(self.run_turn())
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.client.request.assert_awaited_with('turn/interrupt', {'threadId': 'thread', 'turnId': 'turn'}, timeout=5)

    async def test_transport_close_is_detected_without_probe(self):
        self.event('connection/closed')
        with self.assertRaises(CodexUnavailable) as caught:
            await self.run_turn()
        self.assertEqual(caught.exception.stage, 'transport')

    async def test_rpc_error_keeps_code_and_stage_but_not_payload(self):
        async def send(message):
            self.client.pending[message['id']].set_exception(CodexUnavailable.from_error({
                'code': -32603, 'message': 'secret', 'data': {
                    'codexErrorInfo': {'httpConnectionFailed': {'httpStatusCode': 502}}}}, stage='rpc'))
        with patch.object(self.client, '_send', send), self.assertRaises(CodexUnavailable) as caught:
            await CodexClient.request(self.client, 'thread/start', {})
        self.assertEqual(caught.exception.stage, 'thread/start')
        self.assertEqual(caught.exception.rpc_code, -32603)
        self.assertEqual(caught.exception.http_status, 502)
        self.assertNotIn('secret', error_details(caught.exception))
        self.assertFalse(self.client.pending)

    async def test_rpc_timeout_has_stage(self):
        with patch.object(self.client, '_send', AsyncMock()), self.assertRaises(CodexUnavailable) as caught:
            await CodexClient.request(self.client, 'thread/start', {}, timeout=0.001)
        self.assertEqual(caught.exception.stage, 'thread/start')
        self.assertEqual(caught.exception.cause, 'TimeoutError')
        self.assertFalse(self.client.pending)

    async def test_reader_keeps_structured_rpc_error(self):
        client = CodexClient()
        stream = asyncio.StreamReader()
        client.process = SimpleNamespace(stdout=stream)
        future = asyncio.get_running_loop().create_future()
        client.pending[1] = future
        stream.feed_data(json.dumps({'id': 1, 'error': {'code': -32603, 'message': 'secret',
            'data': {'codexErrorInfo': {'httpConnectionFailed': {'httpStatusCode': 502}}}}}).encode() + b'\n')
        stream.feed_eof()
        await client._read()
        with self.assertRaises(CodexUnavailable) as caught:
            await future
        self.assertEqual(caught.exception.http_status, 502)
        self.assertEqual(caught.exception.rpc_code, -32603)
        self.assertNotIn('secret', caught.exception.details())

    async def test_malformed_final_is_not_logged_as_upstream_message(self):
        self.event('item/completed', item={'type': 'agentMessage', 'text': 'private response'})
        self.event('turn/completed', turn={'id': 'turn', 'status': 'completed'})
        with self.assertRaises(CodexUnavailable) as caught:
            await self.run_turn()
        self.assertEqual(caught.exception.stage, 'format')
        self.assertEqual(caught.exception.cause, 'JSONDecodeError')
        self.assertNotIn('private response', caught.exception.details())

    async def test_error_metadata_rejects_arbitrary_remote_strings(self):
        for info in ('secret', {'secret': {'httpStatusCode': 'secret'}}, {'httpConnectionFailed': {'httpStatusCode': True}}):
            error = CodexUnavailable.from_error({'message': 'secret', 'code': 'secret',
                                                 'codexErrorInfo': info}, stage='turn')
            self.assertNotIn('secret', error.details())
            self.assertIsNone(error.http_status)
            self.assertIsNone(error.rpc_code)


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_unresponsive_rpc_retires_process_only_after_other_calls_finish(self):
        client = CodexClient()
        client.process = SimpleNamespace(returncode=None)
        second_started, finish_second = asyncio.Event(), asyncio.Event()
        thread_count = 0
        async def request(method, params, **kwargs):
            nonlocal thread_count
            if method == 'thread/start':
                thread_count += 1
                return {'thread': {'id': str(thread_count)}}
            return {}
        async def run_turn(thread_id, *args, **kwargs):
            if thread_id == '1':
                await second_started.wait()
                raise CodexUnavailable(stage='turn/start', cause='TimeoutError')
            second_started.set()
            await finish_second.wait()
            return {'topics': [], 'terms_to_check': []}, {}
        with patch.object(client, 'ensure_started', AsyncMock()), patch.object(client, 'request', request), patch.object(
                client, '_run_turn', run_turn), patch.object(client, '_close', AsyncMock()) as close:
            first = asyncio.create_task(client.complete('test', {}))
            second = asyncio.create_task(client.complete('test', {}))
            with self.assertRaises(CodexUnavailable):
                await first
            self.assertEqual(client.active_calls, 1)
            close.assert_not_awaited()
            finish_second.set()
            await second
            close.assert_awaited_once()
        self.assertEqual(client.active_calls, 0)
        self.assertFalse(client.restart_when_idle)

    async def test_failed_batch_does_not_kill_parallel_batch_and_success_clears_cooldown(self):
        client = CodexClient()
        client.process = SimpleNamespace(returncode=None)
        second_started, finish_second = asyncio.Event(), asyncio.Event()
        thread_count = 0

        async def request(method, params, **kwargs):
            nonlocal thread_count
            if method == 'thread/start':
                thread_count += 1
                return {'thread': {'id': str(thread_count)}}
            return {}

        async def run_turn(thread_id, *args, **kwargs):
            if thread_id == '1':
                await second_started.wait()
                raise CodexUnavailable(stage='turn', http_status=500)
            second_started.set()
            await finish_second.wait()
            return {'topics': [], 'terms_to_check': []}, {}

        with patch.object(client, 'ensure_started', AsyncMock()), patch.object(client, 'request', request), patch.object(
                client, '_run_turn', run_turn), patch.object(client, 'close', AsyncMock()) as close:
            first = asyncio.create_task(client.complete('test', {}))
            second = asyncio.create_task(client.complete('test', {}))
            with self.assertRaises(CodexUnavailable) as caught:
                await first
            self.assertEqual(caught.exception.http_status, 500)
            self.assertFalse(client.available_for_retry())
            self.assertFalse(second.done())
            self.assertIn('2', client.events)
            finish_second.set()
            _, usage = await second
            self.assertEqual(usage['backend'], 'codex')
            self.assertTrue(client.available_for_retry())
            close.assert_not_awaited()
        self.assertFalse(client.events)

    async def test_total_timeout_interrupts_turn_and_keeps_shared_process(self):
        client = CodexClient()
        client.process = SimpleNamespace(returncode=None)
        async def request(method, params, **kwargs):
            if method == 'thread/start':
                return {'thread': {'id': 'thread'}}
            if method == 'turn/start':
                return {'turn': {'id': 'turn'}}
            return {}
        with patch.dict(os.environ, {'AI_CODEX_TIMEOUT_SECONDS': '0.01'}), patch.object(
                client, 'ensure_started', AsyncMock()), patch.object(client, 'request', AsyncMock(side_effect=request)
                ) as rpc, patch.object(client, 'close', AsyncMock()) as close:
            with self.assertRaises(CodexUnavailable) as caught:
                await client.complete('test', {})
        self.assertEqual(caught.exception.stage, 'turn')
        self.assertEqual(caught.exception.cause, 'TimeoutError')
        self.assertEqual([call.args[0] for call in rpc.await_args_list],
                         ['thread/start', 'turn/start', 'turn/interrupt', 'thread/unsubscribe'])
        self.assertFalse(client.available_for_retry())
        self.assertFalse(client.events)
        close.assert_not_awaited()

    async def test_cancellation_does_not_activate_cooldown(self):
        client = CodexClient()
        client.process = SimpleNamespace(returncode=None)
        started = asyncio.Event()
        async def request(method, params, **kwargs):
            return {'thread': {'id': 'thread'}} if method == 'thread/start' else {'turn': {'id': 'turn'}}
        async def consume(*args):
            started.set()
            await asyncio.Event().wait()
        with patch.object(client, 'ensure_started', AsyncMock()), patch.object(client, 'request', AsyncMock(
                side_effect=request)) as rpc, patch.object(client, '_consume_turn', consume), patch.object(
                client, 'close', AsyncMock()) as close:
            task = asyncio.create_task(client.complete('test', {}))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual([call.args[0] for call in rpc.await_args_list][-2:], ['turn/interrupt', 'thread/unsubscribe'])
        self.assertTrue(client.available_for_retry())
        self.assertFalse(client.events)
        close.assert_not_awaited()

    async def test_error_messages_are_not_copied_to_logs(self):
        client = CodexClient()
        with patch.object(client, 'ensure_started', AsyncMock(side_effect=ValueError('Bearer secret'))), self.assertLogs(
                'src.common.chat_summary_codex', level='WARNING') as logs:
            with self.assertRaises(CodexUnavailable) as caught:
                await client.complete('test', {})
        self.assertEqual(caught.exception.cause, 'ValueError')
        self.assertNotIn('secret', ' '.join(logs.output))

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

    async def test_flash_truncation_logs_metrics_without_chat_content(self):
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = httpx.Response(200, json={
            'usage': {'completion_tokens': 8000},
            'choices': [{'finish_reason': 'length', 'message': {'content': 'private chat content'}}]})
        with patch('src.common.chat_summary.api_config', return_value=('https://example.com', 'fake', 'flash')), patch(
                'src.common.chat_summary.httpx.AsyncClient', return_value=client), self.assertLogs(
                'src.common.chat_summary', level='WARNING') as logs:
            with self.assertRaisesRegex(RuntimeError, '输出上限'):
                await _qwen_complete({}, {}, fallback=True)
        self.assertIn('completion_tokens=8000', ' '.join(logs.output))
        self.assertNotIn('private chat content', ' '.join(logs.output))
        self.assertEqual(client.post.await_count, 1)

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

    async def test_cooldown_started_while_waiting_for_start_lock_is_not_extended(self):
        client = CodexClient()
        expected = time.monotonic() + 123
        async def ensure():
            client.retry_after = expected
            raise CodexUnavailable(stage='cooldown')
        with patch.object(client, 'ensure_started', ensure), self.assertRaises(CodexUnavailable):
            await client.complete('test', {})
        self.assertEqual(client.retry_after, expected)

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
    def complete_fallback(self, group=1):
        records = rows()
        self.insert(records, group=group)
        bid = db.stage_batch(group, records, self.path)
        claim = db.claim_batch(bid, db_path=self.path)
        value = result()
        value['needs_primary_reanalysis'] = True
        db.finish_batch(bid, value, db.revision(self.path), claim['token'], self.path)
        return bid, records

    async def test_recovery_rotates_by_last_attempt_instead_of_newest_batch(self):
        now = int(time.time())
        bids = [self.complete_fallback(group=group)[0] for group in (1, 2, 3)]
        with db.connection(self.path) as conn:
            for index, bid in enumerate(bids):
                conn.execute('UPDATE chat_summary_batch SET created_at=?,end_ts=? WHERE id=?',
                             (now - 500 + index * 100, now - 500 + index * 100, bid))
        self.assertEqual([item['batch_id'] for item in db.recovery_candidates(self.path)], bids)
        claim = db.claim_recovery(bids[0], self.path)
        db.release_recovery(bids[0], claim['claim_token'], self.path)
        # 即使冷却已结束，刚尝试的批次仍在另两个等待更久的批次之后。
        candidates = db.recovery_candidates(self.path, now=now + 400)
        self.assertEqual([item['batch_id'] for item in candidates], [bids[1], bids[2], bids[0]])

    async def test_incoming_batch_does_not_jump_ahead_of_waiting_retries(self):
        now = int(time.time())
        older, _ = self.complete_fallback()
        newer, _ = self.complete_fallback(group=2)
        with db.connection(self.path) as conn:
            conn.execute('UPDATE chat_summary_recovery SET claimed_at=?,attempts=10 WHERE batch_id=?',
                         (now - 600, older))
            conn.execute('UPDATE chat_summary_batch SET created_at=? WHERE id=?', (now, newer))
        self.assertEqual([item['batch_id'] for item in db.recovery_candidates(self.path)], [older, newer])

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


class RecoveryJobTests(unittest.IsolatedAsyncioTestCase):
    async def test_job_uses_queue_order_and_logs_sanitized_failure(self):
        from src.spider.jobs import chat_summary as jobs
        error = CodexUnavailable('private message', stage='turn', kind='httpConnectionFailed', http_status=500)
        with patch.dict(os.environ, {'AI_BACKEND': 'codex'}), patch.object(
                jobs.db, 'init_db'), patch.object(jobs.db, 'recovery_candidates', return_value=[
                    {'batch_id': 74, 'group_id': 1}, {'batch_id': 78, 'group_id': 1}]), patch.object(
                jobs, 'retry_ready', return_value=True), patch.object(jobs, 'group_ids', return_value=[1]), patch.object(
                jobs, 'recover_primary_summary', AsyncMock(side_effect=error)) as recover, self.assertLogs(
                jobs.logger, level='INFO') as logs:
            await jobs.recover_chat_summaries()
        recover.assert_awaited_once_with(74)
        text = ' '.join(logs.output)
        self.assertIn('复查开始 batch=74', text)
        self.assertIn('http_status=500', text)
        self.assertNotIn('private message', text)

    async def test_cooldown_skips_model_requests(self):
        from src.spider.jobs import chat_summary as jobs
        with patch.dict(os.environ, {'AI_BACKEND': 'codex'}), patch.object(
                jobs.db, 'init_db'), patch.object(jobs.db, 'recovery_candidates', return_value=[
                    {'batch_id': 74, 'group_id': 1}]), patch.object(jobs, 'retry_ready', return_value=False), patch.object(
                jobs, 'recover_primary_summary', AsyncMock()) as recover:
            await jobs.recover_chat_summaries()
        recover.assert_not_awaited()
