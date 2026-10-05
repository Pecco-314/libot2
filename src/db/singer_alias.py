"""查歌手的精确别名；别名直接指向曲库中的歌手名，不串联解析。"""
from src.common.singers import split_singers
from src.db.sqlite import DEFAULT_DB_PATH, connect_sqlite, write_transaction

DEFAULT_ALIASES = {
    "邓紫棋": "G.E.M. 邓紫棋",
    "三理": "三理Mit3uri",
    "七海": "七海Nana7mi",
    "艾因": "艾因Eine",
    "羽音": "羽音Hanon",
    "琉绮": "琉绮Ruki",
    "无理": "无理Muri",
    "一果": "一果Ichigo",
    "光一": "中单光一",
    "弥希": "弥希Miki",
    "星弥": "星弥Hoshimi",
    "罗伊": "罗伊Roi",
    "阿萨": "阿萨Aza",
    "千春": "千春Chiharu",
    "小可": "小可学妹",
    "阿梓": "阿梓从小就很可爱",
    "勾檀": "勾檀Mayumi",
    "舒三妈": "舒三妈Susam",
    "诺莺": "诺莺Nox",
    "桃星": "桃星Tocci",
    "瑞娅": "瑞娅_Rhea",
    "尤格": "尤格Yog",
    "希侑": "希侑Kiyuu",
    "雾深": "雾深Girimi",
    "雪绘": "雪绘Yukie",
    "莱恩": "莱恩Leo",
}


def init_singer_alias_db(db_path=DEFAULT_DB_PATH):
    with write_transaction(db_path) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS singer_alias (
            alias_key TEXT PRIMARY KEY, alias TEXT NOT NULL, singer TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        conn.executemany("INSERT OR IGNORE INTO singer_alias(alias_key,alias,singer) VALUES(?,?,?)",
                         [(alias.casefold(), alias, singer) for alias, singer in DEFAULT_ALIASES.items()])


def resolve_singer_alias(singer, *, conn=None, db_path=DEFAULT_DB_PATH):
    value = singer.strip()
    if conn is None:
        with connect_sqlite(db_path) as connection:
            return resolve_singer_alias(value, conn=connection)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='singer_alias'").fetchone():
        row = conn.execute("SELECT singer FROM singer_alias WHERE alias_key=?", (value.casefold(),)).fetchone()
        if row:
            return row[0]
    return DEFAULT_ALIASES.get(value, value)


def add_singer_alias(alias, singer, db_path=DEFAULT_DB_PATH):
    alias, singer = alias.strip(), singer.strip()
    if not alias or not singer or len(alias)>80 or len(singer)>120:
        raise ValueError("别名和歌手名不能为空，且不能过长")
    init_singer_alias_db(db_path)
    with write_transaction(db_path) as conn:
        known = {part.casefold(): part for row in conn.execute("SELECT DISTINCT original_singer FROM song_info")
                 for part in split_singers(row[0])}
        canonical = known.get(singer.casefold())
        if canonical is None:
            raise ValueError("曲库中没有这个完整歌手名，请填写歌曲原唱中的单个歌手名")
        if alias.casefold() in known and alias.casefold() != canonical.casefold():
            raise ValueError("这个别名已经是曲库中另一位歌手的名字")
        previous = conn.execute("SELECT singer FROM singer_alias WHERE alias_key=?", (alias.casefold(),)).fetchone()
        if previous:
            if previous[0].casefold() != canonical.casefold():
                raise ValueError(f"这个别名已经指向：{previous[0]}")
            return canonical, False
        if alias.casefold() == canonical.casefold():
            return canonical, False
        conn.execute("INSERT INTO singer_alias(alias_key,alias,singer) VALUES(?,?,?)", (alias.casefold(),alias,canonical))
        return canonical, True
