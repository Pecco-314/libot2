# QQ 表情回应排行榜

群内命令：

- `贴表情榜`、`被贴表情榜`：默认本月，前30名，尽量合为一张图，极长时拆页。
- `贴表情榜 今天`、`被贴表情榜 近7天`。
- `贴表情榜 2026-09`、`被贴表情榜 2026-09-01 2026-09-24`。

日期使用北京时间，结束日期包含当天。只允许查询当前群。每个人显示总次数和数量最多的前10种表情，总次数仍按全部表情计算。当前群名片优先，未知名字显示“未知昵称”；图片不显示QQ号及页脚说明。

同一人给同一消息贴同一表情计一次。重复添加通知不累加，也不改变添加时间。统计在所选时间内添加、截至查询区间终点仍有效的回应，取消的不计；取消后重新添加按重新添加时间计算。通知的 `count` 是消息上该表情的累计数，不能作为该用户的贡献累加。秒级时间相同、操作类型也相同的重复通知合并。

数据保存在 `data/qq_reactions.db`，与直播数据库隔离。消息作者按机器人账号、群号和消息ID定位，只保存ID、时间和名字，不保存聊天正文。无法确认作者的回应只计入贴表情榜。历史日志可能有缺口，不能视为完整群历史。

恢复存量：

```sh
.venv/bin/python scripts/backfill_qq_reactions.py
.venv/bin/python scripts/backfill_qq_reactions.py --apply
```

导入可重复执行，按事件身份去重。插件启用后直接监听 `group_msg_emoji_like` 通知；原消息作者缺失时有限并发请求 `get_msg`，并校验群号。35天以前、没有任何回应记录引用的消息作者每天清理。

QQ表情图片取自 [Koishi QFace](https://github.com/koishijs/QFace) 整理的腾讯原版资源，按其索引中的 `emojiId` 匹配，缓存至 `data/assets/qq_emoji`，版权归腾讯。资源地址为 `https://koishi.js.org/QFace/assets/qq_emoji/<ID>/png/<文件名>.png`，GitHub raw 作备用。Unicode 表情优先使用同源QQ资源，缺失时使用本地 Twemoji；QQ专有表情下载失败时显示名称，未知名称显示表情ID。名称兜底映射来自 NapCat 的 `packages/napcat-core/external/face_config.json`。QQ表情ID与QQ空间的表情编号存在差异，旧表情只使用配置中核实过的 `qzoneCode` 映射，例如炸弹为 `e170.gif`。
