# NapCat 标准输出日志

`libotctl start napcat` 使用 `src/log_supervisor.py` 启动并监督 NapCat。工作目录、QQ 启动参数和代理设置沿用 libotctl 配置；此机制不依赖 NapCat 的 `fileLog`。

- 标准输出和标准错误合流写入 `logs/napcat.log`，保留原始字节。
- 活跃文件达到 20 MiB 后轮转为 `napcat.log.日期时间-纳秒.gz`。优先在换行处分片，单条超长消息可能跨片。
- 启动时已有文件达到阈值，会完整归档为一个文件，不拆改历史内容。
- 压缩成功后才移除对应未压缩副本；失败时保留原始归档。历史归档不自动清理，应另行规划保留期限与磁盘告警。
- `napcat.log.rotate.lock` 是文件锁载体，不是 PID 文件；服务退出后文件可以保留，内核会释放锁。同一日志只允许一个监督进程写入。
- `napcat.pid` 记录监督进程。停止信号由监督进程转发至独立的 NapCat 子进程组，并继续排空管道；子进程退出后监督进程退出并清理同组残留进程。原有 PID 命名空间、命令行、工作目录和进程组校验继续生效。
- `libotctl log napcat` 识别 inode 变化，轮转后跟随新的活跃文件。

修改代码不会切换正在运行的旧进程的日志描述符。下一次经授权的 NapCat 启动使用此机制；不要为生效而停止 monitor、capture 等其他服务。异常断电、强制 SIGKILL 或磁盘写满不具备无损保证。

回归测试：`.venv/bin/python -m unittest tests.test_log_supervisor tests.test_libotctl -q`。测试只启动临时的 Python 子进程，不操作实际 QQ 服务。
