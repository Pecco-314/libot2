from nonebot import on_notice
from nonebot.adapters.onebot.v11 import Bot, GroupDecreaseNoticeEvent

leave_notice = on_notice(priority=50, block=False)

@leave_notice.handle()
async def handle_group_decrease(bot: Bot, event: GroupDecreaseNoticeEvent):
    user_id = event.user_id
    try:
        user_info = await bot.get_stranger_info(user_id=user_id)
        name = user_info.get("nickname", str(user_id))
    except Exception:
        name = str(user_id)
        
    await bot.send_group_msg(group_id=event.group_id, message=f"{name}离开了我们...")
