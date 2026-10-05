import unittest

from scripts.audit_danmaku_clock_bursts import find_bursts, singleton_index


def rows(times):
    return [dict(id=i, uid=i % 10 + 1, content=f'测试消息{i:04d}', time=t)
            for i, t in enumerate(times)]


class ClockBurstTests(unittest.TestCase):
    def test_compressed_backlog_detected(self):
        normal = list(range(0, 600, 10))
        compressed = normal[:30] + [600 + i / 10 for i in range(30)]
        hits = find_bursts(singleton_index(rows(compressed)), singleton_index(rows(normal)))
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]['anchors'], 30)

    def test_constant_offset_not_a_burst(self):
        normal = list(range(0, 600, 10))
        self.assertFalse(find_bursts(singleton_index(rows([t+200 for t in normal])),
                                     singleton_index(rows(normal))))

    def test_real_message_spike_not_a_burst(self):
        times = [i / 10 for i in range(100)]
        self.assertFalse(find_bursts(singleton_index(rows(times)), singleton_index(rows(times))))

    def test_same_user_repeats_are_not_anchors(self):
        data = [dict(id=i, uid=1, content='谁家小孩', time=i) for i in range(20)]
        self.assertFalse(singleton_index(data))

    def test_one_user_cannot_establish_a_burst(self):
        a, b = rows([500+i/10 for i in range(30)]), rows(list(range(0, 300, 10)))
        for r in a+b:
            r['uid'] = 1
        self.assertFalse(find_bursts(singleton_index(a), singleton_index(b)))


if __name__ == '__main__':
    unittest.main()
