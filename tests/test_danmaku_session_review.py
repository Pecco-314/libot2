import unittest

from scripts.review_mit3uri_danmaku_sessions import (
    fit_model, is_validation, predicted_offset, review_session, unambiguous_pairs,
)


def anchors():
    return [(i*10.,12.-i*.01,i+1,i+1000,i+2000) for i in range(100)]


class SessionReviewTest(unittest.TestCase):
    def test_archived_anchors_do_not_become_deletion_candidates(self):
        baseline=[];replay=[]
        for i in range(100):
            text=f'独立的长句校时锚点{i}'
            baseline.append(dict(id=2000+i,uid=i+1,content=text,timestamp=10000+i*10,match_kind=None))
            replay.append(dict(event_id=1000+i,uid=i+1,content=text,cid=1,bvid='bv',progress_ms=i*10000,mapped_timestamp_ms=(10012+i*10)*1000,match_kind='inserted'))
        accepted,*_=review_session(replay,baseline,set(),candidate_ids={1000})
        self.assertEqual(accepted,{1000:2000})
        accepted,reasons,*_=review_session(replay,baseline,set(),candidate_ids={1000},claimed_baselines={2000})
        self.assertFalse(accepted)
        self.assertEqual(reasons[1000],'original_already_claimed')

    def test_linear_drift_validated_by_other_users(self):
        model=fit_model(anchors())
        self.assertIsNotNone(model)
        self.assertEqual(model['validation_share'],1.)
        self.assertAlmostEqual(predicted_offset(model,500),11.5)

    def test_validation_users_can_reject_training_fit(self):
        aa=[(x,y+20 if is_validation(uid) else y,uid,r,b) for x,y,uid,r,b in anchors()]
        self.assertIsNone(fit_model(aa))
        self.assertIsNone(fit_model(aa,max_residual=4.5))

    def test_jitter_bound_is_fitted_and_capped(self):
        aa=[(x,y+((uid*37)%71)/10.-3.5,uid,r,b) for x,y,uid,r,b in anchors()]
        self.assertIsNone(fit_model(aa))
        model=fit_model(aa,max_residual=4.5)
        self.assertIsNotNone(model)
        self.assertLessEqual(model['tolerance'],4.5)
        self.assertGreaterEqual(model['validation_share'],.9)

    def test_abrupt_jump_is_not_treated_as_one_part_model(self):
        aa=[(x,y+40 if x>450 else y,uid,r,b) for x,y,uid,r,b in anchors()]
        self.assertIsNone(fit_model(aa))

    def test_unanchored_gap_is_not_interpolated(self):
        model=fit_model([a for a in anchors() if a[0]<200 or a[0]>800])
        self.assertIsNotNone(model)
        self.assertIsNone(predicted_offset(model,500))
        self.assertIsNone(predicted_offset(model,1200))

    def test_matching_is_one_to_one_without_greedy_choices(self):
        self.assertEqual(unambiguous_pairs({1:[9],2:[9],3:[10,11],4:[11],5:[12]}),{5:12})

    def test_reserved_originals_protect_repeated_messages(self):
        replays=[];baseline=[]
        for i in range(100):
            text=f'这是独特测试长句第{i}条'
            baseline.append(dict(id=i+2000,uid=i+1,content=text,timestamp=10000+i*10,match_kind=None))
            replays.append(dict(event_id=i+1000,uid=i+1,content=text,cid=1,bvid='bv',progress_ms=i*10000,
                                mapped_timestamp_ms=(10000+i*10+12)*1000,match_kind='inserted'))
        # Two replay occurrences and one original: it may be a genuine repeat.
        baseline.append(dict(id=9999,uid=999,content='真实连续发送的消息',timestamp=10500,match_kind='existing'))
        replays.append(dict(event_id=9999,uid=999,content='真实连续发送的消息',cid=1,bvid='bv',progress_ms=500000,
                            mapped_timestamp_ms=10512000,match_kind='existing'))
        replays.append(dict(event_id=9998,uid=999,content='真实连续发送的消息',cid=1,bvid='bv',progress_ms=501000,
                            mapped_timestamp_ms=10513000,match_kind='inserted'))
        accepted,reasons,_,parts,_=review_session(replays,baseline,set())
        self.assertEqual(len(accepted),100)
        self.assertNotIn(9998,accepted)
        self.assertEqual(reasons[9998],'original_already_claimed')
        self.assertEqual(parts[0]['reserved_conflicts'],1)


if __name__=='__main__':unittest.main()
