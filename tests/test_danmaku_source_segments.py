import unittest

from scripts.plan_danmaku_source_segments import choose_primary, segments, map_replay_sessions, classify


class SourceSegmentsTest(unittest.TestCase):
    def test_session_identity_uses_bvid_not_reused_sequential_id(self):
        mapped,ambiguous=map_replay_sessions([dict(session_id='001',bvid='BVfirst')],{'BVfirst':'002','BVother':'001'})
        self.assertEqual(mapped,{'001':'002'})
        self.assertFalse(ambiguous)

    def test_split_catalog_session_is_not_mapped_arbitrarily(self):
        mapped,ambiguous=map_replay_sessions([dict(session_id='001',bvid='a'),dict(session_id='001',bvid='b')],{'a':'002','b':'003'})
        self.assertFalse(mapped)
        self.assertEqual(ambiguous,{'001':['002','003']})

    def test_single_source_silence_does_not_prove_incompleteness(self):
        counts={'danmakus:a':{0:10}}
        plan=segments(counts,'danmakus:a',0,360)
        self.assertEqual(classify(counts,'danmakus:a',plan,[]),'single_available_source')

    def test_overlapping_same_provider_streams_are_not_summed(self):
        counts={'danmakus:a':{0:10,1:10},'danmakus:b':{0:8,1:8},'vtbcat:c':{0:10,1:10,2:10}}
        self.assertEqual(choose_primary(counts),'vtbcat:c')

    def test_equal_coverage_prefers_danmakus(self):
        self.assertEqual(choose_primary({'danmakus':{0:95,1:100},'vtbcat':{0:100,1:95}}),'danmakus')

    def test_more_complete_source_beats_priority(self):
        self.assertEqual(choose_primary({'danmakus':{0:100},'vtbcat':{0:100,1:100,2:100}}),'vtbcat')

    def test_low_volume_does_not_imply_low_coverage(self):
        self.assertEqual(choose_primary({'danmakus':{0:100,1:10,2:10},'replay':{0:1000}}),'danmakus')

    def test_sustained_missing_span_is_gap_candidate(self):
        result=segments({'danmakus':{0:10,3:10},'vtbcat':{0:10,1:10,2:10,3:10}},'danmakus',0,480)
        self.assertEqual([r['status'] for r in result],['primary','gap_fill','primary'])
        self.assertEqual(result[1]['source'],'vtbcat')

    def test_silence_and_short_gap_are_not_automatic_fill(self):
        result=segments({'danmakus':{0:10},'vtbcat':{1:10}},'danmakus',0,360)
        self.assertEqual([r['status'] for r in result],['primary','short_gap_review','all_sources_silent_unknown'])

    def test_partial_capture_is_preserved_for_review(self):
        result=segments({'danmakus':{0:40},'vtbcat':{0:100}},'danmakus',0,120)
        self.assertEqual(result[0]['status'],'partial_deficit_review')


if __name__=='__main__':unittest.main()
