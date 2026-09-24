# 手动合并网站歌单

预览（仅下载网站快照、读取本地数据库）：

```sh
.venv/bin/python scripts/sync_song_list_confirm.py
```

输出位于 `data/song_sync_review/`：CSV 网站快照、Markdown 差异清单，以及对应 JSON 执行计划。
JSON 中每项的 `selected` 决定是否执行，可逐项改为 `true` 或 `false`；Markdown 是阅读报告，不控制执行。

默认选中补入次数、补歌切和空字段。已有元数据冲突、减少次数、未匹配歌曲新增均需人工选择。
同一天唱两次按两次处理；网站日期范围之后的本地演唱记录保留。歌词不参与合并。
网站不提供演唱顺序，因此补入的记录追加，已有记录 ID 和相对顺序不变。

确认执行指定计划（不重新抓网站）：

```sh
.venv/bin/python scripts/sync_song_list_confirm.py --confirm data/song_sync_review/review-日期时间.json
```

执行前核对歌单表快照指纹。若预览后本地发生变化，拒绝执行，需重新生成计划。
写入前保存 `song_info`、`song_record` 完整逻辑备份 JSON（含歌词），变更在一个事务内完成。
其他表、定时同步开关和线上服务不变。备份可用于按原 ID 恢复，但不得直接覆盖已经产生后续修改的歌单。

别名人工映射：准备 JSON，例如 `{"900": 902}`（网站序号 → 本地歌曲 ID），通过
`--mapping 路径 --csv 网站快照.csv` 重新生成预览。网站序号不能当作本地 ID。
原有自动合并中的历史日期相似度推断未用于此脚本；标题唯一匹配、同名歌手消歧和显式映射才参与合并。
