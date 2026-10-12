"""命令及别名统一注册，强制参数前有空格。"""

from __future__ import annotations

from nonebot import on_command as _on_command

COMMAND_MODULE_NAME = __name__


_COMMAND_CANONICAL_NAMES: dict[str, str] = {}


def on_command(command: str, *args, aliases=None, **kwargs):
    alias_set = set(aliases or ())
    canonical_name = command.strip()
    for name in {canonical_name, *alias_set}:
        _COMMAND_CANONICAL_NAMES[name.casefold()] = canonical_name
    return _on_command(
        command,
        *args,
        aliases=alias_set,
        force_whitespace=True,
        **kwargs,
    )


help_cmd = on_command("帮助", priority=5)


superchat_cmd = on_command("查SC", aliases={"查sc", "查Sc"}, priority=5, block=True)


manager_help_cmd = on_command("管理员帮助", priority=5, block=True)


feature_enable_cmd = on_command("打开功能", priority=5, block=True)


feature_disable_cmd = on_command("关闭功能", priority=5, block=True)


feature_status_cmd = on_command("功能状态", priority=5, block=True)


cooldown_set_cmd = on_command("设置CD", aliases={"设置cd"}, priority=5, block=True)


sub_show_cmd = on_command("查看订阅", priority=5, block=True)


sub_set_cmd = on_command("设置订阅", aliases={"订阅直播"}, priority=5, block=True)


sub_remove_cmd = on_command("删除订阅", aliases={"取消订阅"}, priority=5, block=True)


nickname_set_cmd = on_command("设置昵称", priority=5, block=True)


name_history_cmd = on_command("曾用名", aliases={"查曾用名"}, priority=5, block=True)


medal_wall_cmd = on_command("查牌子", priority=5, block=True)


chat_summary_cmd = on_command("在聊什么", priority=5, block=True)


abbreviation_query_cmd = on_command("查缩写", priority=5, block=True)


abbreviation_add_cmd = on_command("新增缩写", priority=5, block=True)


abbreviation_remove_cmd = on_command("删除缩写", priority=5, block=True)


choose_cmd = on_command("抉择", aliases={"choose"}, priority=5, block=True)


meme_add_cmd = on_command("新增梗", aliases={"addmeme"}, priority=5, block=True)


meme_image_add_cmd = on_command(
    "新增图片", aliases={"addimage"}, priority=5, block=True
)


meme_alias_cmd = on_command("新增别名", aliases={"addalias"}, priority=5, block=True)


meme_remove_cmd = on_command("删除梗", aliases={"delmeme"}, priority=5, block=True)


meme_query_cmd = on_command("来点", priority=5, block=True)


meme_rank_cmd = on_command("memerank", priority=5, block=True)


fans_trend_cmd = on_command("查粉丝", priority=5, block=True)


guards_trend_cmd = on_command("查舰长", aliases={"查大航海"}, priority=5, block=True)


club_trend_cmd = on_command("查粉丝团", priority=5, block=True)


fan_club_list_cmd = on_command("粉丝团", aliases={"粉丝团列表"}, priority=5, block=True)


concurrent_cmd = on_command("查同接", priority=5, block=True)


blind_box_cmd = on_command("查盲盒", priority=5, block=True)


danmaku_rank_cmd = on_command("弹幕榜", priority=5, block=True)


reaction_rank_cmd = on_command("贴表情榜", priority=5, block=True)


reaction_received_rank_cmd = on_command("被贴表情榜", priority=5, block=True)


events_cmd = on_command("查弹幕", priority=5, block=True)


content_search_cmd = on_command("有谁说过", aliases={"谁说过"}, priority=5, block=True)


song_search_cmd = on_command("查歌曲", priority=5, block=True)


song_singer_cmd = on_command("查歌手", priority=5, block=True)


singer_alias_cmd = on_command("新增歌手别名", priority=5, block=True)


song_list_cmd = on_command("查歌单", priority=5, block=True)


random_search_cmd = on_command("随机歌曲", priority=5, block=True)


now_playing_cmd = on_command("在唱什么", aliases={"正在演唱"}, priority=5, block=True)


song_preview_cmd = on_command("预览歌单", priority=5, block=True)


dc_cmd = on_command("斗虫", priority=5, block=True)


live_list_add_cmd = on_command("添加直播", aliases={"增加直播"}, priority=5, block=True)


live_list_remove_cmd = on_command("删除直播", priority=5, block=True)


live_list_show_cmd = on_command("开播列表", aliases={"开播"}, priority=5, block=True)


live_list_tag_members_cmd = on_command(
    "查看标签", aliases={"标签列表"}, priority=5, block=True
)


live_list_add_tag_cmd = on_command(
    "添加标签", aliases={"增加标签"}, priority=5, block=True
)


live_list_set_tag_cmd = on_command("修改标签", priority=5, block=True)


live_sessions_cmd = on_command("直播记录", aliases={"查直播"}, priority=5, block=True)


clip_marker_cmd = on_command("切片", priority=5, block=True)


clip_document_cmd = on_command("切片文档", priority=5, block=True)


activity_list_cmd = on_command("查动态", priority=5, block=True)


activity_generate_cmd = on_command("生成动态", priority=5, block=True)


cmd_add_song = on_command("新增歌曲", priority=5, block=True)


cmd_generate_list = on_command("生成歌单", priority=5, block=True)


cmd_update_list = on_command("提交歌单", aliases={"提交"}, priority=5, block=True)


def _canonical_command_name(name: str) -> str | None:
    return _COMMAND_CANONICAL_NAMES.get(name.strip().lstrip("/").casefold())
