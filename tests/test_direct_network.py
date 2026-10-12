import ast
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from src import libotctl


class DirectNetworkTests(unittest.TestCase):
    def test_http_clients_require_explicit_proxy_isolation(self):
        root = Path(__file__).resolve().parents[1] / 'src'
        checked = 0
        for path in root.rglob('*.py'):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if not isinstance(node.func.value, ast.Name):
                    continue
                if (node.func.value.id, node.func.attr) not in {
                    ('httpx', 'Client'), ('httpx', 'AsyncClient'), ('aiohttp', 'ClientSession'),
                }:
                    continue
                keywords = {kw.arg: kw.value for kw in node.keywords}
                with self.subTest(path=str(path.relative_to(root)), line=node.lineno):
                    self.assertIn('trust_env', keywords)
                    self.assertIsInstance(keywords['trust_env'], ast.Constant)
                    self.assertIs(keywords['trust_env'].value, False)
                    if path.name != 'chat_summary_codex.py':
                        self.assertNotIn('proxy', keywords)
                checked += 1
        self.assertGreater(checked, 20)

    def test_service_launch_removes_proxy_but_keeps_codex_setting(self):
        proxies = {'HTTP_PROXY': 'http://127.0.0.1:17890',
                   'https_proxy': 'http://127.0.0.1:17890',
                   'ALL_PROXY': 'http://127.0.0.1:17890',
                   'WSS_PROXY': 'http://127.0.0.1:17890',
                   'NO_PROXY': 'localhost',
                   'AI_CODEX_PROXY_URL': 'http://127.0.0.1:17892'}
        with tempfile.TemporaryDirectory() as directory:
            for name in libotctl.MODULES:
                spec = replace(libotctl.MODULES[name], pid_file=Path(directory)/(name+'.pid'),
                               log_file=Path(directory)/(name+'.log'))
                process = Mock(pid=12345)
                process.poll.return_value = None
                with self.subTest(name=name), patch.dict(os.environ, proxies), \
                        patch.object(libotctl, '_status', return_value=(libotctl.ProcessState.DEAD, None)), \
                        patch.object(libotctl, '_resolve_command', return_value=['test']), \
                        patch.object(libotctl.subprocess, 'Popen', return_value=process) as popen, \
                        patch.object(libotctl.time, 'sleep'):
                    self.assertEqual(libotctl._start(spec), 0)
                    env = popen.call_args.kwargs['env']
                    for key in proxies:
                        if key.lower().endswith('_proxy') and key != 'NO_PROXY':
                            self.assertNotIn(key, env)
                    self.assertEqual(env['NO_PROXY'], '*')
                    self.assertEqual(env['no_proxy'], '*')
                    self.assertEqual(env['AI_CODEX_PROXY_URL'], proxies['AI_CODEX_PROXY_URL'])
