from __future__ import annotations


BLIND_BOX_COST_COIN = 15_000
BLIND_BOX_DATA_START = "2026-04-11"

# Values and icons come from Bilibili's official 心动盲盒 rules and gift config.
# `coin` uses the live API's raw unit: 1,000 coin = 1 CNY.
BLIND_BOX_GIFTS: dict[str, dict[str, str | int]] = {
    "电影票": {
        "coin": 2_000,
        "icon": "https://s1.hdslb.com/bfs/live/20864a10beaea541c7dce264d5bbc56676d63e4f.png",
    },
    "棉花糖": {
        "coin": 9_000,
        "icon": "https://s1.hdslb.com/bfs/live/b555682af41551c28f8ad19dc5c4ed87943c84f4.png",
    },
    "爱心抱枕": {
        "coin": 16_000,
        "icon": "https://s1.hdslb.com/bfs/live/824714c830966d7bec381e35ef808b1f478e21ee.png",
    },
    "绮彩权杖": {
        "coin": 40_000,
        "icon": "https://s1.hdslb.com/bfs/live/5cecbf274a4205ef76ed3f11c6540f0c6743363c.png",
    },
    "时空之站": {
        "coin": 100_000,
        "icon": "https://s1.hdslb.com/bfs/live/9ee53aedda3c891fdf23d35c14b3bdc4e0504a97.png",
    },
    "神驹宝玺": {
        "coin": 200_000,
        "icon": "https://s1.hdslb.com/bfs/live/0ab2b3d221338a5376db2eaf45fae0dc03ecb87d.png",
    },
    "浪漫城堡": {
        "coin": 2_233_000,
        "icon": "https://s1.hdslb.com/bfs/live/4ee13c275e13b0a7404bd8af035775db3c721b86.png",
    },
}

BLIND_BOX_RESULT_ORDER = tuple(BLIND_BOX_GIFTS)
