# 斗虫 SC 历史补充

`import_qianqiu_sc.py` 从公开 `GET https://vr.qianqiuzy.cn/gift/sc` 按月串行读取三理的SC明细，默认范围为2025年11月至当前月。

```sh
.venv/bin/python scripts/import_qianqiu_sc.py --fetch --directory data/imports/qianqiu_sc_20260924
.venv/bin/python scripts/import_qianqiu_sc.py --apply --directory data/imports/qianqiu_sc_20260924
```

目录保存原始响应、核对报告、实际插入ID及导入后验证结果。重复运行使用缓存并核对已存在事件，不重复入库。SC 使用北京时间，金额以元存入 `total_coin`，不会乘1000。

去重按UID、标准化内容、金额与时间进行，优先精确匹配，再一对一匹配3秒内的时间偏差。双方完整身份各自唯一时，可匹配5分钟内的采集延迟；歧义或同用户同时刻内容/金额冲突的记录写入报告，暂不插入。不同日期再次发送的相同内容仍可独立保留。

只补充 `event` 中缺失的 `SUPER_CHAT_MESSAGE`，并补充其用户名索引，不覆盖已有事件。每批最多100条，取得写锁后再次核对；无需停止或重启服务。新增记录涉及的日期图片缓存会移入该次报告的 `image_cache_backup`，供查询重新生成，原缓存可以恢复。按用户查询的图片本身会每次重绘。
