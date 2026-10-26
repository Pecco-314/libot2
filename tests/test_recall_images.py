"""撤回图片下载域名与失败诊断。"""
import ast
import asyncio
import logging
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlparse

import httpx


def download_namespace(assets):
    tree = ast.parse(Path('src/plugins/recall.py').read_text())
    nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.name in {'allowed_image_url', 'download'}]
    env = dict(urlparse=urlparse, ASSETS=assets, asyncio=asyncio,
               downloads=asyncio.Semaphore(4), time=time, db=SimpleNamespace(TTL=86400),
               httpx=httpx, logger=logging.getLogger('recall_test'))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<recall>', 'exec'), env)
    return env


class RecallImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_allowed_hosts(self):
        check = download_namespace(Path('/tmp'))['allowed_image_url']
        for host in ('multimedia.nt.qq.com.cn', 'gchat.qpic.cn', 'gxh.vip.qq.com'):
            self.assertTrue(check('https://' + host + '/image'))
        for url in ('http://multimedia.nt.qq.com.cn/image', 'https://qq.com.evil.test/a',
                    'https://multimedia.nt.qq.com.cn.evil.test/a', 'file:///tmp/image'):
            self.assertFalse(check(url))

    async def test_download_and_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            assets = Path(temp)
            env = download_namespace(assets)
            client = httpx.AsyncClient
            def factory(**kwargs):
                return client(**kwargs, transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, content=b'image-data')))
            with patch.object(httpx, 'AsyncClient', side_effect=factory):
                await env['download']('https://multimedia.nt.qq.com.cn/image', 'ok.img', time.time())
            self.assertEqual((assets / 'ok.img').read_bytes(), b'image-data')
            with self.assertLogs('recall_test', level='WARNING') as logs:
                await env['download']('https://example.com/image?secret=value', 'bad.img', time.time())
            self.assertIn('unsupported_url', logs.output[0])
            self.assertNotIn('secret', logs.output[0])
