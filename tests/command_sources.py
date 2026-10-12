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
