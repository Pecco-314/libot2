from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, Event

help_cmd = on_command("帮助", priority=5)

@help_cmd.handle()
async def handle_help(bot: Bot, event: Event):
    help_text = (
        "/帮助 - 显示帮助信息\n"
    )
    await help_cmd.finish(help_text)