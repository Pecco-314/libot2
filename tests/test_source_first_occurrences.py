import unittest

from scripts.continue_source_first_occurrences import reconcile_occurrences,clock_anchors,clock_diagnosis


def source(i,t):return dict(id=i,uid=1,content='晚安',time=t,raw_time=t)
def event(i,t,aligned=None,kind=None):return dict(id=i,uid=1,content='晚安',timestamp=t,time=t if aligned is None else aligned,raw_time=t,aligned=aligned is not None,match_kind=kind)


class OccurrenceTests(unittest.TestCase):
    def test_same_source_same_second_repeats_preserved(self):
        pairs,drops,a,b=reconcile_occurrences([source(1,100),source(2,100)],[event(10,100),event(11,100)])
        self.assertEqual(len(pairs),2);self.assertFalse(drops or a or b)

    def test_ordered_retimes_preserve_both_occurrences(self):
        pairs,drops,a,b=reconcile_occurrences([source(1,100),source(2,102)],[event(10,105,100),event(11,107,102)])
        self.assertEqual([(x['id'],y['id']) for x,y in pairs],[(1,10),(2,11)]);self.assertFalse(drops or a or b)

    def test_surplus_native_is_not_deleted(self):
        pairs,drops,a,b=reconcile_occurrences([source(1,100)],[event(10,100),event(11,101,100)])
        self.assertFalse(drops);self.assertEqual(len(b),1)

    def test_only_supported_surplus_replay_is_removable(self):
        pairs,drops,a,b=reconcile_occurrences([source(1,100)],[event(10,100),event(11,105,100,'inserted')])
        self.assertEqual([(d['id'],k['id']) for d,k in drops],[(11,10)]);self.assertFalse(a or b)

    def test_ambiguous_repeat_not_deleted(self):
        pairs,drops,a,b=reconcile_occurrences([source(1,100),source(2,102)],[event(10,100),event(11,102),event(12,106,101,'inserted')])
        self.assertFalse(drops);self.assertEqual(len(b),1)

    def test_cross_boundary_is_matched_on_whole_timeline(self):
        pairs,drops,a,b=reconcile_occurrences([source(1,100)],[event(10,96,100)])
        self.assertEqual(len(pairs),1);self.assertFalse(drops or a or b)

    def test_missing_anchors_explained(self):
        info=clock_diagnosis([],None,event(10,100))
        self.assertEqual(info['nearby_train'],0);self.assertIn('锚点不足',info['reason'])

    def test_seven_second_tolerance_requires_clock_and_preserves_count(self):
        a=[source(1,107)]
        b=[event(10,100,100)]
        self.assertEqual(len(reconcile_occurrences(a,b)[0]),0)
        pairs,drops,aa,bb=reconcile_occurrences(a,b,tolerance=7)
        self.assertEqual(len(pairs),1);self.assertFalse(drops or aa or bb)
        self.assertFalse(reconcile_occurrences(a,[event(10,100)],tolerance=7)[0])

    def test_empty_source_retains_existing_rows_for_review(self):
        pairs,drops,a,b=reconcile_occurrences([],[event(10,100,100)],tolerance=7)
        self.assertFalse(pairs or drops or a);self.assertEqual(len(b),1)


if __name__=='__main__':unittest.main()
