import unittest

from scripts.review_remaining_danmaku_sequences import safe_occurrences, ordered_bracket


class OccurrenceReviewTest(unittest.TestCase):
    def setUp(self):
        self.replay={1:dict(cid=10,progress_ms=1000,mapped_timestamp_ms=110000),2:dict(cid=10,progress_ms=2000,mapped_timestamp_ms=111000)}
        self.base={8:dict(timestamp=100),9:dict(timestamp=101)}

    def test_two_actual_sends_must_map_to_two_originals(self):
        self.assertEqual(safe_occurrences({1:[8],2:[8]},[],self.replay,self.base),{})
        self.assertEqual(safe_occurrences({1:[8],2:[9]},[],self.replay,self.base),{1:8,2:9})

    def test_unknown_occurrence_reserves_nearby_originals(self):
        self.assertEqual(safe_occurrences({1:[8]},[2],self.replay,self.base),{})

    def test_reversed_send_order_is_rejected(self):
        self.assertEqual(safe_occurrences({1:[9],2:[8]},[],self.replay,self.base),{})

    def test_bracket_requires_six_ordered_independent_messages(self):
        aa=[(i*5,0,i,i,i) for i in range(6)]
        bb={i:dict(timestamp=100+i*2) for i in range(6)}
        bracket=ordered_bracket(aa,12,999,bb)
        self.assertEqual(bracket[:2],(103,107))
        self.assertIsNone(ordered_bracket(aa,12,0,bb))
        bb[4]['timestamp']=100
        self.assertIsNone(ordered_bracket(aa,12,999,bb))

    def test_bracket_does_not_bridge_long_gaps(self):
        aa=[(i*5,0,i,i,i) for i in range(6)]
        bb={i:dict(timestamp=100+i*20) for i in range(6)}
        bb[3]['timestamp']+=1
        self.assertIsNone(ordered_bracket(aa,12,999,bb))


if __name__=='__main__':unittest.main()
