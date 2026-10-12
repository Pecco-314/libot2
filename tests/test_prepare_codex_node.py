import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from scripts import prepare_codex_node as script


class NodePreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source.yaml'
        self.destination = self.root / 'private'
        self.source.write_text(yaml.safe_dump({
            'tun': {'enable': True}, 'allow-lan': True, 'mixed-port': 7890,
            'external-controller': '0.0.0.0:9090',
            'proxies': [{'name': 'chosen', 'type': 'ss', 'server': 'example.com',
                         'port': 443, 'cipher': 'aes-128-gcm', 'password': 'test-only'}]}))

    def tearDown(self):
        self.temporary.cleanup()

    def run_script(self, *options):
        with patch.object(sys, 'argv', ['prepare_codex_node', str(self.source), *options]), patch.object(
            script, 'DESTINATION', self.destination), patch.object(
            script.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as command, patch('builtins.print') as out:
            script.main()
        return command, out

    def test_preview_does_not_write_or_execute(self):
        command, out = self.run_script()
        self.assertFalse(self.destination.exists())
        command.assert_not_called()
        self.assertNotIn('test-only', str(out.call_args_list))

    def test_only_the_selected_node_is_saved_without_tun_or_controller(self):
        command, out = self.run_script('--apply')
        saved = self.destination / 'config.yaml'
        config = yaml.safe_load(saved.read_text())
        self.assertEqual(config['mixed-port'], 17892)
        self.assertFalse(config['tun']['enable'])
        self.assertFalse(config['allow-lan'])
        self.assertNotIn('external-controller', config)
        self.assertEqual(config['proxies'][0]['name'], 'chosen')
        self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.destination.stat().st_mode & 0o777, 0o700)
        self.assertEqual(command.call_count, 1)
        self.assertEqual(command.call_args.args[0][:2], ['/usr/local/bin/mihomo', '-t'])
        self.assertNotIn('test-only', str(out.call_args_list))
