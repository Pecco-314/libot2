"""拆分合作署名，保留名称本身含分隔符的音乐组合。"""
import re

SLASHED_SINGER_NAMES = ("K/DA", "HUNTR/X")
PROTECTED_SINGER_NAMES = SLASHED_SINGER_NAMES + (
    "Selena Gomez & The Scene",
    "The Mamas & The Papas",
)
SINGER_SEPARATOR = re.compile(r"\s*(?:/|&|＆|,|，|、|×|\b(?:feat|ft)\.(?=\s)|\bfeaturing\b)\s*", re.IGNORECASE)

# 成员查询包含关联组合的作品；组合查询仍只匹配组合署名。
SINGER_GROUP_MEMBERS = {
    "Selena Gomez & The Scene": ("Selena Gomez",),
}


def singer_query_names(singer: str) -> set[str]:
    """展开已解析别名的歌手查询，保留原始署名用于展示。"""
    normalized = singer.strip().casefold()
    if not normalized:
        return set()
    return {normalized} | {
        group.casefold()
        for group, members in SINGER_GROUP_MEMBERS.items()
        if normalized in {member.casefold() for member in members}
    }


def split_singers(value):
    text = str(value or "")
    protected = {}
    for index, name in enumerate(PROTECTED_SINGER_NAMES):
        token = f"\x00{index}\x00"
        pattern = r"(?<!\w)" + re.escape(name).replace(r"\&", "[&＆]") + r"(?!\w)"
        text = re.sub(pattern, lambda _: token, text, flags=re.IGNORECASE)
        protected[token] = name
    return list(dict.fromkeys(protected.get(part.strip(), part.strip())
                             for part in SINGER_SEPARATOR.split(text) if part.strip()))
