# LiBot2

基于 NapCat + NoneBot2 的QQ机器人，提供了与三理Mit3uri（或其他主播）直播相关的各种功能。

## 模块

本项目分为多个互相独立的模块，每个模块独立后台运行。通过libotctl来管理模块的运行。

1. napcat：接收QQ消息。
2. libot：处理接收的消息并回复，不负责具体的功能实现。
3. spider：通过Bilibili API定时获取动态、粉丝数等信息。
4. monitor：通过blivedm监控直播间并实时记录弹幕等信息。
5. web: Web管理界面。