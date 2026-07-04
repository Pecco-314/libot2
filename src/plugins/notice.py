from nonebot import on_notice
from nonebot.adapters.onebot.v11 import Bot, GroupDecreaseNoticeEvent, GroupIncreaseNoticeEvent

from src.db.subscription import get_subscription_feature

# leave notice
leave_notice = on_notice(priority=50, block=False)


@leave_notice.handle()
async def handle_group_decrease(bot: Bot, event: GroupDecreaseNoticeEvent):
    if getattr(event, "sub_type", None) == "kick_me":
        return

    group_id = int(event.group_id)
    if not get_subscription_feature(group_id, "leave_notice"):
        return

    user_id = event.user_id
    try:
        user_info = await bot.get_stranger_info(user_id=user_id)
        name = user_info.get("nickname", str(user_id))
    except Exception:
        name = str(user_id)

    await bot.send_group_msg(group_id=event.group_id, message=f"{name}离开了我们...")


# join notice
join_notice = on_notice(priority=50, block=False)


@join_notice.handle()
async def handle_group_increase(bot: Bot, event: GroupIncreaseNoticeEvent):
    group_id = int(event.group_id)
    if not get_subscription_feature(group_id, "join_notice"):
        return

    user_id = event.user_id
    try:
        user_info = await bot.get_stranger_info(user_id=user_id)
        name = user_info.get("nickname", str(user_id))
    except Exception:
        name = str(user_id)

    # 使用 CQ at 提及新成员并发送欢迎消息
    await bot.send_group_msg(group_id=event.group_id, message=f"[CQ:at,qq={user_id}] 欢迎新人！")
