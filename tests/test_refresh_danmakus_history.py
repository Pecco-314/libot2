import unittest

from scripts.refresh_danmakus_history import canonical, catalog_versions, match_occurrences, match_resolved_emojis


def row(ts, text='晚安', uid=1):
    return dict(uid=uid, norm=text, timestamp_ms=ts)


class RefreshDanmakusTest(unittest.TestCase):
    def test_upstream_emoji_representations(self):
        assert canonical('room_1967216004_84389', {'room_1967216004_84389': '晚安'}) == '晚安'
        assert canonical('upower_[鸣潮_晚安]', {}) == '[鸣潮_晚安]'
        assert canonical('[upower_[鸣潮_晚安]]', {}) == '[鸣潮_晚安]'
        assert canonical('[鸣潮_晚安]', {}) == '[鸣潮_晚安]'
        assert canonical('别把upower_[文字]改掉', {}) == '别把upower_[文字]改掉'
        assert canonical('official_348', {}) == '[official_348]'
        assert canonical('room_123_456', {}) == '[room_123_456]'
        assert canonical('[room_123_456]', {}) == '[room_123_456]'


    def test_exact_occurrences_are_reserved_before_near_matches(self):
        assert match_occurrences([row(1000), row(2000)], [row(2000)], 1000) == {1: 0}


    def test_repeated_messages_preserve_multiplicity(self):
        pairs = match_occurrences([row(1000), row(1000), row(1000)], [row(1000), row(1000)], 0)
        assert len(pairs) == 2
        assert len(set(pairs.values())) == 2


    def test_user_text_and_time_must_match(self):
        assert match_occurrences([row(1000)], [row(1000, uid=2), row(1000, text='拜拜'), row(2001)], 1000) == {}

    def test_resolved_emoji_requires_unique_exact_uid_and_time(self):
        new = [dict(row(1000, '[表情_名称]'), content='upower_[表情_名称]')]
        old = [row(1000, '[/bfs/emote/example.png]')]
        assert match_resolved_emojis(new, old, {}) == {0: 0}
        assert match_resolved_emojis(new * 2, old, {}) == {}
        assert match_resolved_emojis(new, old * 2, {}) == {}
        assert match_resolved_emojis(new, [row(1001, '[/bfs/emote/example.png]')], {}) == {}
        assert match_resolved_emojis(new, [row(1000, '普通文字')], {}) == {}
        assert match_resolved_emojis(new, old, {0: 0}) == {}

    def test_catalog_includes_alternative_uploads(self):
        alternate = dict(liveId='upload', startDate=1000, danmakusCount=20)
        primary = dict(liveId='primary', startDate=1000, danmakusCount=10,
                       versions=[dict(live=alternate)])
        future = dict(liveId='protected', startDate=1800000000000, versions=[])
        versions = catalog_versions(dict(data=dict(lives=[primary, future])))
        assert set(versions) == {'upload', 'primary'}
        assert versions['upload']['danmakusCount'] == 20
