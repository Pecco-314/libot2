"""读取命令实现的语法树，避免单元测试启动机器人或初始化生产数据库。"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def command_source_files():
    plugins = ROOT / "src" / "plugins"
    return [
        plugins / "command_registry.py",
        plugins / "command_policy.py",
        plugins / "command_support.py",
        *sorted((plugins / "command_handlers").glob("*.py")),
    ]


def command_tree():
    body = []
    for source in command_source_files():
        body.extend(ast.parse(source.read_text(), filename=str(source)).body)
    return ast.Module(body=body, type_ignores=[])


def text_sender(forward, direct):
    """使用真实折叠逻辑，以纯文本断言测试命令的传输结果。"""
    from types import SimpleNamespace
    from unittest.mock import patch
    from src.plugins.utils import send_foldable_message

    async def send(bot, event, content, **kwargs):
        async def send_direct(event, message):
            return await direct(message.extract_plain_text())

        async def send_forward(_bot, event, nodes):
            nodes = [{**n, 'data': {**n['data'], 'content': n['data']['content'].extract_plain_text()}} for n in nodes]
            return await forward(bot, event, nodes)

        transport = SimpleNamespace(self_id=bot.self_id, send=send_direct)
        with patch('src.plugins.utils.send_forward_message', send_forward):
            return await send_foldable_message(transport, event, content, **kwargs)
    return send
