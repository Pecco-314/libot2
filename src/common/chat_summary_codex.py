"""服务器端 Codex stdio 长连接；各次总结使用独立的临时会话。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import ssl
import time
import tomllib
import weakref
from pathlib import Path

import httpx

from src.common.utils import ROOT
from src.common.chat_summary_images import ImageCatalog, MAX_PER_CALL


class CodexUnavailable(RuntimeError):
    """连接、认证或模型调用失败，可使用备用模型。"""


def response_schema(*, image_requests=False):
    def item(properties):
        return {"type": "object", "additionalProperties": False,
                "properties": {**{key: {"type": "string"} for key in properties},
                               "message_ids": {"type": "array", "items": {"type": "string"}}},
                "required": [*properties, "message_ids"]}

    schema = {"type": "object", "additionalProperties": False,
            "properties": {
                "topics": {"type": "array", "items": item(("title", "summary"))},
                "terms_to_check": {"type": "array", "items": item(("term", "possible_meaning"))}},
            "required": ["topics", "terms_to_check"]}
    if image_requests:
        schema["properties"]["image_requests"] = {
            "type": "array", "maxItems": MAX_PER_CALL, "items": {"type": "string"}}
        schema["required"].append("image_requests")
    return schema


def process_config():
    """总结子进程可使用PC跳板或独立出口，不修改交互式Codex配置。"""
    binary = os.environ.get("AI_CODEX_BINARY", str(
        Path.home() / ".local/lib/codex-runtime/node_modules/.bin/codex"))
    bridge = os.environ.get("AI_CODEX_USE_PC_BRIDGE", "false").lower() == "true"
    config = {"project_doc_max_bytes": 0, "web_search": "disabled", "notify": []}
    tier = os.environ.get("AI_CODEX_SERVICE_TIER", "fast")
    if tier not in {"fast", "default"}:
        raise ValueError("AI_CODEX_SERVICE_TIER需为fast或default")
    config.update(service_tier=tier)
    config["features.fast_mode"] = tier == "fast"
    # 不向群聊分析器开放宿主工具、插件或项目指令。
    for feature in ("shell_tool", "unified_exec", "apps", "plugins", "hooks",
                    "multi_agent", "memories", "browser_use", "browser_use_external",
                    "computer_use", "image_generation", "view_image", "code_mode",
                    "code_mode_host", "sleep_tool", "workspace_dependencies", "skill_search",
                    "unbounded_connection_retries"):
        config[f"features.{feature}"] = False
    auth_root = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    config_path = auth_root / "config.toml"
    if config_path.exists():
        settings = tomllib.loads(config_path.read_text())
        for name in settings.get("mcp_servers", {}):
            config[f"mcp_servers.{name}.enabled"] = False
    if bridge:
        config.update({
            "model_provider": "libot-pc-bridge",
            "chatgpt_base_url": "http://127.0.0.1:17891/backend-api/",
            "model_providers.libot-pc-bridge.name": "Libot PC bridge",
            "model_providers.libot-pc-bridge.base_url": "http://127.0.0.1:17891/backend-api/codex",
            "model_providers.libot-pc-bridge.wire_api": "responses",
            "model_providers.libot-pc-bridge.requires_openai_auth": True,
        })
    else:
        config.update(model_provider="openai", chatgpt_base_url="https://chatgpt.com/backend-api/")
    allowed = {'HOME', 'PATH', 'USER', 'LOGNAME', 'SHELL', 'LANG', 'TZ', 'TMPDIR', 'CODEX_HOME', 'SSL_CERT_DIR'}
    env = {k: v for k, v in os.environ.items() if k in allowed or k.startswith('LC_')}
    proxy = os.environ.get("AI_CODEX_PROXY_URL", "").strip()
    if bridge:
        proxy = "http://127.0.0.1:17890"
        env["SSL_CERT_FILE"] = str(Path.home() / ".local/share/codex/ca-bundle.pem")
    if proxy:
        url = httpx.URL(proxy)
        if url.scheme != "http" or url.host not in {"127.0.0.1", "localhost", "::1"} or url.userinfo:
            raise ValueError("AI_CODEX_PROXY_URL需为本机HTTP代理，不接受远程代理或凭据")
        if not bridge and url.port in {17890, 17891}:
            raise ValueError("常驻总结禁止使用依赖PC的转发端口")
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "WSS_PROXY"):
            env[name] = env[name.lower()] = proxy
    if bundle := os.environ.get('AI_CODEX_CA_BUNDLE', '').strip():
        env['SSL_CERT_FILE'] = bundle
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost,::1"
    args = [binary, "app-server", "--listen", "stdio://"]
    for key, value in config.items():
        args += ["-c", f"{key}={json.dumps(value, ensure_ascii=False)}"]
    return args, env, bridge, proxy


class CodexClient:
    def __init__(self):
        self.process = None
        self.reader = None
        self.pending = {}
        self.events = {}
        self.next_id = 0
        self.start_lock = asyncio.Lock()
        self.write_lock = asyncio.Lock()
        self.retry_after = 0.0

    def available_for_retry(self):
        return time.monotonic() >= self.retry_after

    async def check_network(self, env, bridge, proxy):
        """匿名探测实际出口，不调用模型；401表示已连接到需要认证的后端。"""
        bundle = env.get('SSL_CERT_FILE')
        verify = ssl.create_default_context(cafile=bundle) if bundle else True
        path = '/backend-api/codex/models?client_version=0.160.1'

        async def probe(url, proxy_url=None):
            async with httpx.AsyncClient(proxy=proxy_url, trust_env=False,
                                         timeout=10, verify=verify) as client:
                response = await client.get(url)
                if response.status_code not in {200, 400, 401}:
                    raise CodexUnavailable("Codex出口不可用")

        try:
            # PC模式同时检查普通HTTP代理和Codex后端转发，避免仅监听仍在被当成在线。
            async with asyncio.timeout(10):
                if bridge:
                    await asyncio.gather(probe('http://127.0.0.1:17891' + path),
                                         probe('https://chatgpt.com' + path, proxy))
                else:
                    await probe('https://chatgpt.com' + path, proxy or None)
        except (httpx.HTTPError, TimeoutError):
            raise CodexUnavailable("Codex出口不可达") from None

    async def _send(self, message):
        if not self.process or self.process.returncode is not None:
            raise CodexUnavailable("Codex连接未运行")
        async with self.write_lock:
            self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
            await self.process.stdin.drain()

    async def request(self, method, params, timeout=30):
        self.next_id += 1
        request_id = self.next_id
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self._send({"id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(request_id, None)

    async def _read(self):
        process = self.process
        try:
            while line := await process.stdout.readline():
                message = json.loads(line)
                if "id" in message and "method" not in message:
                    future = self.pending.get(message["id"])
                    if future and not future.done():
                        if "error" in message:
                            # 上游错误可能带请求正文或身份信息，不转发原始错误文本。
                            future.set_exception(CodexUnavailable("Codex RPC请求失败"))
                        else:
                            future.set_result(message.get("result", {}))
                elif "id" in message:
                    await self._send({"id": message["id"], "error": {
                        "code": -32601, "message": "Tools and approvals are disabled"}})
                else:
                    params = message.get("params", {})
                    queue = self.events.get(params.get("threadId"))
                    if queue:
                        queue.put_nowait(message)
        except (asyncio.CancelledError, Exception):
            pass
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(CodexUnavailable("Codex连接已断开"))
            for queue in self.events.values():
                queue.put_nowait({"method": "connection/closed"})

    async def ensure_started(self):
        async with self.start_lock:
            if not self.available_for_retry():
                raise CodexUnavailable("Codex等待恢复")
            args, env, bridge, proxy = process_config()
            # 每次生成前都检查；常驻进程存活不能证明PC没有休眠或SSH链路没有断开。
            await self.check_network(env, bridge, proxy)
            if self.process and self.process.returncode is None and self.reader and not self.reader.done():
                return
            if self.process:
                await self._close()
            workdir = ROOT / "data" / "chat_summary_codex"
            workdir.mkdir(mode=0o700, exist_ok=True)
            self.process = await asyncio.create_subprocess_exec(
                *args, cwd=workdir, env=env, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=32 * 1024 * 1024)
            self.reader = asyncio.create_task(self._read())
            await self.request("initialize", {"clientInfo": {
                "name": "libot_chat_summary", "version": "0.1.0"}})
            await self._send({"method": "initialized"})

    async def close(self):
        async with self.start_lock:
            await self._close()

    async def _close(self):
        process, reader = self.process, self.reader
        if process and process.returncode is None:
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except asyncio.TimeoutError:
                # 只终止本实例创建的总结子进程，不查找或修改任何线上PID文件。
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
        if reader:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
        self.process = self.reader = None

    async def _run_turn(self, thread_id, inputs, *, effort, tier, image_requests):
        await self.request("turn/start", {
            "threadId": thread_id, "model": os.environ.get("AI_CODEX_MODEL", "gpt-6.1-sol"),
            "effort": effort, "serviceTier": tier, "input": inputs,
            "outputSchema": response_schema(image_requests=image_requests),
            "approvalPolicy": "never", "sandboxPolicy": {"type": "readOnly", "networkAccess": False}})
        final, tokens = None, {}
        queue = self.events[thread_id]
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), 30)
            except asyncio.TimeoutError:
                _, env, bridge, proxy = process_config()
                await self.check_network(env, bridge, proxy)
                continue
            method, params = event["method"], event.get("params", {})
            if method == "connection/closed":
                raise CodexUnavailable("Codex连接已断开")
            if method == 'error':
                raise CodexUnavailable('Codex响应流失败')
            if method in {"item/started", "item/completed"}:
                item = params.get("item", {})
                if item.get("type") in {"commandExecution", "fileChange", "mcpToolCall",
                                        "dynamicToolCall", "webSearch", "imageGeneration"}:
                    raise CodexUnavailable("总结会话出现不允许的工具调用")
                if method == "item/completed" and item.get("type") == "agentMessage":
                    if item.get("phase") in {None, "final_answer", "finalAnswer"}:
                        final = item.get("text")
            elif method == "thread/tokenUsage/updated":
                tokens = params.get("tokenUsage", {}).get("last", {})
            elif method == "turn/completed":
                if params.get("turn", {}).get("status") != "completed" or not final:
                    raise CodexUnavailable("Codex未完成总结")
                break
        result = json.loads(final)
        if not isinstance(result, dict) or any(not isinstance(result.get(key), list)
                                               for key in ('topics', 'terms_to_check')):
            raise CodexUnavailable('Codex总结格式无效')
        return result, tokens

    async def complete(self, prompt, packed, *, image_sources=None):
        if not self.available_for_retry():
            raise CodexUnavailable("Codex等待恢复")
        model = os.environ.get("AI_CODEX_MODEL", "gpt-6.1-sol")
        effort = os.environ.get("AI_CODEX_EFFORT", "high")
        tier = os.environ.get("AI_CODEX_SERVICE_TIER", "fast")
        catalog = ImageCatalog(image_sources) if image_sources else None
        thread_id = None
        try:
            await self.ensure_started()
            params = {
                "model": model, "ephemeral": True, "sandbox": "read-only",
                "approvalPolicy": "never", "baseInstructions": prompt,
                "developerInstructions": "仅分析输入中的聊天数据。禁止使用工具、访问文件或网络。只返回要求的JSON。"
                                         + ("本批有图片可按需读取。理解讨论依赖图片时，先在image_requests里返回消息meta.images中的编号，"
                                            "每次最多4张；无需图片时返回空列表。随后会收到所选图片的图像输入，"
                                            "或明确的读取失败说明，再完成摘要。图片尚未加载不等于无法读取。" if catalog else ""),
                "cwd": str(ROOT / "data" / "chat_summary_codex"),
                "serviceName": "libot_chat_summary", "serviceTier": tier}
            thread = await self.request("thread/start", params)
            thread_id = thread["thread"]["id"]
            self.events[thread_id] = asyncio.Queue()
            inputs = [{"type": "text", "text": json.dumps(packed, ensure_ascii=False, separators=(',', ':'))}]
            costs, can_request = [], bool(catalog)
            timeout = float(os.environ.get("AI_CODEX_TIMEOUT_SECONDS", "600"))
            async with asyncio.timeout(timeout):
                for round_index in range(4):
                    requests_allowed = can_request and round_index < 3
                    result, tokens = await self._run_turn(thread_id, inputs, effort=effort,
                                                          tier=tier, image_requests=requests_allowed)
                    costs.append(tokens)
                    requested = result.pop('image_requests', [])
                    if not requests_allowed or not requested:
                        break
                    images = await asyncio.to_thread(catalog.read, {'image_ids': requested})
                    can_request = images['success']
                    inputs = [{"type": "text", "text": "以下是你请求的图片读取结果。结合本会话原消息完成总结，"
                               "证据编号沿用原消息。已请求的图片不要重复请求；无更多必要图片时结束。"
                               + ("图片读取未成功，本轮不再请求，保留不确定。" if not can_request else "")}]
                    for content in images['contentItems']:
                        inputs.append({"type": "image", "url": content['imageUrl']} if content['type'] == 'inputImage'
                                      else {"type": "text", "text": content['text']})
            return result, {
                "backend": "codex", "model": model,
                "reasoning_effort": effort, "service_tier": tier,
                "image_read_requests": catalog.call_count if catalog else 0,
                "images_read": catalog.read_count if catalog else 0,
                "turn_count": len(costs),
                "prompt_tokens": sum(t.get("inputTokens", 0) for t in costs),
                "completion_tokens": sum(t.get("outputTokens", 0) for t in costs),
                "total_tokens": sum(t.get("totalTokens", 0) for t in costs),
                "prompt_tokens_details": {"cached_tokens": sum(t.get("cachedInputTokens", 0) for t in costs)},
                "reasoning_tokens": sum(t.get("reasoningOutputTokens", 0) for t in costs)}
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception:
            self.retry_after = time.monotonic() + float(os.environ.get("AI_CODEX_RETRY_SECONDS", "300"))
            await self.close()
            raise CodexUnavailable("Codex不可用，等待网络或认证恢复") from None
        finally:
            if thread_id:
                if self.process and self.process.returncode is None:
                    with contextlib.suppress(Exception):
                        await self.request("thread/unsubscribe", {"threadId": thread_id}, timeout=5)
                self.events.pop(thread_id, None)


_clients = weakref.WeakKeyDictionary()


def get_client():
    loop = asyncio.get_running_loop()
    if loop not in _clients:
        _clients[loop] = CodexClient()
    return _clients[loop]


def retry_ready():
    return get_client().available_for_retry()


async def complete(prompt, packed, *, image_sources=None):
    return await get_client().complete(prompt, packed, image_sources=image_sources)
