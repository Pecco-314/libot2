import unittest

from scripts.preview_danmaku_source_replacement import primary_stream,plan_cells,diff_rows,coverage_conflict


class ReplacementPreviewTest(unittest.TestCase):
    def test_replay_volume_never_elects_primary_over_native(self):
        self.assertEqual(primary_stream({'danmakus:a':{0:5},'replay:1':{0:50000,1:50000}}),'danmakus:a')
        self.assertIsNone(primary_stream({'replay:1':{0:50}}))

    def test_sustained_native_coverage_beats_more_rows(self):
        self.assertEqual(primary_stream({'danmakus:a':{0:10000},'vtbcat:b':{0:10,1:10,2:10}}),'vtbcat:b')

    def test_gap_needs_independent_boundary_users(self):
        counts={'danmakus:a':{0:10,3:10},'vtbcat:b':{0:10,1:10,2:10,3:10}}
        plan=plan_cells(counts,'danmakus:a',0,480,{})
        self.assertIn('gap_boundary_clock_review',[r['status'] for r in plan])
        points={'vtbcat:b':[(90,1),(100,2),(110,3),(365,1),(370,2),(375,3)]}
        plan=plan_cells(counts,'danmakus:a',0,480,points)
        gaps=[r for r in plan if r['status']=='validated_gap_fill']
        self.assertEqual([(r['start'],r['end']) for r in gaps],[(125,355)])

    def test_multiset_keeps_actual_repeated_messages(self):
        wanted=[dict(id=i,uid=3,content='晚安',time=100.) for i in (1,2)]
        current=[dict(id=i,uid=3,content='晚安',timestamp=100,match_kind='inserted' if i==12 else None) for i in (10,11,12)]
        add,drop,kept=diff_rows(wanted,current)
        self.assertEqual(add,[]);self.assertEqual(kept,2);self.assertEqual([r['id'] for r in drop],[12])

    def test_nonempty_native_stream_can_still_be_incomplete(self):
        counts={'danmakus:a':{0:5},'replay:1':{0:100}}
        self.assertEqual(coverage_conflict(counts,'danmakus:a',0,0,120,5,[]),'replay_coverage_conflict_review')
        self.assertEqual(coverage_conflict({},'danmakus:a',0,0,120,10,[dict(match_kind=None)]*30),'original_native_coverage_conflict_review')


if __name__=='__main__':unittest.main()
