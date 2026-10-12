"""从帮助说明识别机器人指令，隔离低信息量的娱乐调用。"""

from __future__ import annotations

import re
from functools import lru_cache

from src.render.help import get_admin_help_sections, get_help_sections


@lru_cache(maxsize=1)
def command_context():
    commands = []
    for section in get_help_sections() + get_admin_help_sections():
        for usage, description in section["items"]:
            commands.append({"usage": usage, "meaning": description})
    return commands


@lru_cache(maxsize=1)
def command_names():
    names = {}
    for command in command_context():
        head = re.split(r"[<\[]", command["usage"], maxsplit=1)[0].strip()
        aliases = re.split(r"\s*/\s*", head)
        canonical = aliases[0]
        for alias in aliases:
            names[alias.casefold()] = alias if " / " in head else canonical
    # 帮助图只列常用写法，保留命令注册中的其他等价名称。
    names.update(
        {
            "帮助": "帮助",
            "管理员帮助": "管理员帮助",
            "查曾用名": "曾用名",
            "查大航海": "查舰长",
            "粉丝团列表": "粉丝团",
            "谁说过": "有谁说过",
            "正在演唱": "在唱什么",
            "开播列表": "开播",
            "查直播": "直播记录",
            "标签列表": "查看标签",
            "增加直播": "添加直播",
            "增加标签": "添加标签",
            "订阅直播": "设置订阅",
            "取消订阅": "删除订阅",
            "在聊什么": "在聊什么",
        }
    )
    return names


def identify_command(text):
    """只识别消息开头的完整命令词，普通聊天中提及命令不受影响。"""
    parts = text.strip().removeprefix("/").split(maxsplit=1)
    return command_names().get(parts[0].casefold()) if parts else None


def low_information_command(text):
    """纯查询和娱乐调用略过；提交素材、切片备注及多行讨论保留。"""
    command = identify_command(text)
    return bool(command and '\n' not in text.strip()
                and command not in {'新增梗', '新增图片', '切片', '生成歌单', '新增歌曲'})
