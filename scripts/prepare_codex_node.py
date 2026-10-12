#!/usr/bin/env python3
"""从私密Mihomo配置选取单个节点，生成仅监听本机的独立Codex出口。"""

import argparse
import os
import subprocess
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / 'data' / 'codex_network'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path, help='本机已有的Mihomo/Clash节点配置文件')
    parser.add_argument('--node', help='与PC当前使用的节点全名一致；多个节点时必填')
    parser.add_argument('--apply', action='store_true', help='验证后保存配置，不启动服务或更改全局代理')
    args = parser.parse_args()
    document = yaml.safe_load(args.source.read_text())
    proxies = document.get('proxies', []) if isinstance(document, dict) else []
    candidates = [p for p in proxies if isinstance(p, dict) and p.get('server')
                  and (not args.node or p.get('name') == args.node)]
    if len(candidates) != 1:
        parser.error('请提供包含静态节点的配置，并用--node指定唯一节点；不会自动选节点或展开订阅')
    node = candidates[0]
    if node.get('dialer-proxy'):
        parser.error('所选节点依赖其他代理，请提供直接出口节点')
    config = {
        'mixed-port': 17892, 'allow-lan': False, 'bind-address': '127.0.0.1',
        'mode': 'rule', 'log-level': 'warning', 'ipv6': False,
        'tun': {'enable': False}, 'proxies': [node],
        'proxy-groups': [{'name': 'CODEX', 'type': 'select', 'proxies': [node['name']]}],
        'rules': ['MATCH,CODEX'],
    }
    if not args.apply:
        print('已识别唯一节点；使用--apply验证并保存。节点凭据不会打印，不启动服务。')
        return
    DESTINATION.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(DESTINATION, 0o700)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=DESTINATION,
                                     suffix='.yaml', delete=False) as handle:
        temporary = Path(handle.name)
        yaml.safe_dump(config, handle, allow_unicode=True, sort_keys=False)
    try:
        checked = subprocess.run(['/usr/local/bin/mihomo', '-t', '-d', str(DESTINATION), '-f', str(temporary)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        if checked.returncode:
            parser.error('节点配置未通过当前Mihomo版本验证；没有替换有效配置')
        temporary.replace(DESTINATION / 'config.yaml')
    finally:
        temporary.unlink(missing_ok=True)
    print('已保存私密配置：data/codex_network/config.yaml。仅监听127.0.0.1:17892，关闭TUN，不改系统路由。')
    print('启动scripts/systemd/libot-codex-proxy.service并确认出口可用后，设置AI_BACKEND=codex、AI_CODEX_PROXY_URL=http://127.0.0.1:17892。')


if __name__ == '__main__':
    main()
