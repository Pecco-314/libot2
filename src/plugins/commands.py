"""命令插件入口：加载注册、通用策略及功能处理器，并兼容原符号导入。"""

from __future__ import annotations

from importlib import import_module

from . import command_policy, command_registry, command_support

_HANDLER_MODULES = (
    import_module("src.plugins.command_handlers.misc"),
    import_module("src.plugins.command_handlers.memes"),
    import_module("src.plugins.command_handlers.meme_templates"),
    import_module("src.plugins.command_handlers.summary"),
    import_module("src.plugins.command_handlers.settings"),
    import_module("src.plugins.command_handlers.users"),
    import_module("src.plugins.command_handlers.abbreviations"),
    import_module("src.plugins.command_handlers.statistics"),
    import_module("src.plugins.command_handlers.events"),
    import_module("src.plugins.command_handlers.live"),
    import_module("src.plugins.command_handlers.songs"),
    import_module("src.plugins.command_handlers.activities"),
    import_module("src.plugins.command_handlers.song_management"),
)
_EXPORT_MODULES = (command_registry, command_policy, command_support, *_HANDLER_MODULES)


def __getattr__(name: str):
    for module in _EXPORT_MODULES:
        if hasattr(module, name):
            return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()).union(*(vars(module) for module in _EXPORT_MODULES)))
