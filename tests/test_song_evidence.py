import unittest
from unittest.mock import patch

from src.common.song_evidence import window_evidence, covered_length, select_preview_entries
from src.capture.guesser import _guess_from_texts


class SongEvidenceTests(unittest.TestCase):
    def test_unique_coverage_does_not_double_count_chorus(self):
        lyrics = '春天的风轻轻吹过我们的脸庞夏天的雨悄悄落在远方的山岗'
        text = '春天的风轻轻吹过我们的脸庞'
        evidence = window_evidence([text, text, '这是一些没有关系的讨论'], [(0, text, .9), (1, text, .9)], lyrics)
        self.assertEqual(evidence['support_count'], 1)
        self.assertEqual(evidence['asr_coverage'], .5)
        self.assertEqual(covered_length(evidence['lyric_spans']), len(text))
        self.assertEqual(covered_length([[0, 10], [5, 15], [0, 10]]), 15)

    def point(self, index, *, support=2, coverage=.7, spans=None, words=None, score=.90):
        start = index * 60
        return {'timestamp': start+30, 'window_start': start, 'window_end': start+60,
                'results': [{'title': '歌', 'singer': '人', 'final_score': score,
                             'support_count': support, 'asr_coverage': coverage,
                             'lyrics_length': 200, 'lyric_spans': spans or [[index*20, index*20+20]],
                             'matched_texts': words or [f'第一句{index}', f'第二句{index}'],
                             'evidence_times': [start+5, start+40], 'preview_quality': .8}]}

    def test_one_high_score_sentence_is_rejected(self):
        self.assertEqual(select_preview_entries([self.point(0, support=1, score=1)], .85), [])

    def test_moderate_phrase_scores_contribute_to_coverage(self):
        texts = ['春天的风轻轻吹过我们的脸庞', '夏天的雨悄悄落在远方的山岗']
        evidence = window_evidence(texts, [(i, text, .6) for i, text in enumerate(texts)], ''.join(texts))
        self.assertEqual(evidence['support_count'], 2)
        self.assertEqual(evidence['asr_coverage'], 1)

    def test_continuous_moderate_scores_survive_lower_default(self):
        result = select_preview_entries([self.point(0), self.point(1)], .85)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['longest_run'], 2)
        self.assertEqual(result[0]['lyric_coverage'], .2)
        self.assertEqual(select_preview_entries([self.point(0), self.point(1)], .95), [])

    def test_distant_mentions_are_rejected(self):
        self.assertEqual(select_preview_entries([self.point(0), self.point(5)], .85), [])

    def test_noisy_asr_is_supported_by_whole_segment(self):
        result = select_preview_entries([self.point(0, coverage=.2), self.point(2, coverage=.2)], .85)
        self.assertEqual(len(result), 1)

    def test_raw_window_evidence_survives_zero_sentence_coverage(self):
        points=[self.point(i,support=0,coverage=0) for i in range(3)]
        for i,p in enumerate(points):
            r=p['results'][0]
            r.update(matched_texts=[],lyric_spans=[],observation_texts=[f'错字较多的歌词{i}',f'另一个歌词片段{i}'],
                     observation_times=[i*60+5,i*60+40])
        self.assertEqual(len(select_preview_entries(points,.85)),1)

    def test_strong_anchor_extends_into_weak_windows(self):
        result=select_preview_entries([self.point(0,score=.76),self.point(1,score=.96),self.point(2,score=.75)],.85)
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['sample_count'],3)
        self.assertEqual(select_preview_entries([self.point(0,score=.76),self.point(1,score=.75)],.85),[])

    def test_ambiguous_candidates_remain_unknown(self):
        points=[self.point(i) for i in range(3)]
        for p in points:
            p['results'].append({**p['results'][0],'title':'另一首','final_score':.89})
        self.assertEqual(select_preview_entries(points,.85),[])

    def test_filtered_interloper_does_not_split_song(self):
        before = self.point(0)
        before['results'][0]['matched_texts'] = ['句一', '句二', '句三']
        middle = self.point(1, support=1)
        middle['results'][0]['title'] = '弱候选'
        middle['results'][0]['matched_texts'] = ['同一句']
        after = self.point(3)
        after['results'][0]['matched_texts'] = ['句四', '句五', '句六']
        result = select_preview_entries([before,middle,after], .85)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['distinct_evidence'], 6)

    def competing_points(self, indices=(0, 1), score=.99, runner=.92):
        points = [self.point(i, score=score) for i in indices]
        for point in points:
            point['results'].append({**point['results'][0], 'title': '另一首', 'final_score': runner})
        return points

    def test_consistent_high_winner_can_confirm_without_large_margin(self):
        result = select_preview_entries(self.competing_points(), .85)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['strong_windows'], 0)
        self.assertEqual(result[0]['consecutive_high_windows'], 2)

    def test_consistent_confirmation_requires_adjacent_windows(self):
        self.assertEqual(select_preview_entries(self.competing_points((0, 2)), .85), [])

    def test_consistent_confirmation_respects_threshold_and_ambiguity(self):
        self.assertEqual(select_preview_entries(self.competing_points(), 1), [])
        self.assertEqual(select_preview_entries(self.competing_points(runner=.98), .85), [])
        self.assertEqual(select_preview_entries(self.competing_points(score=.90, runner=.83), .85), [])

    def test_consistent_confirmation_requires_direct_lyric_evidence(self):
        points = self.competing_points()
        for point in points:
            point['results'][0]['lyric_spans'] = [[0, 5]]
        self.assertEqual(select_preview_entries(points, .85), [])

    def test_consistent_confirmation_does_not_join_across_unknown_window(self):
        points = self.competing_points((0, 2))
        points.insert(1, {'timestamp': 90, 'window_start': 60, 'window_end': 120, 'results': []})
        self.assertEqual(select_preview_entries(points, .85), [])

    def test_coverage_is_soft_but_repeated_sentence_is_rejected(self):
        self.assertEqual(len(select_preview_entries([self.point(i, spans=[[0, 8]]) for i in range(3)], .85)), 1)
        self.assertEqual(select_preview_entries([self.point(i, words=['同一句']) for i in range(3)], .85), [])

    def test_instant_recognition_keeps_legacy_output(self):
        info = {'title': '歌', 'original_singer': '人', 'lyrics_cleaned': '春天的风轻轻吹过我们的脸庞'}
        with patch('src.capture.guesser._ensure_matcher'), patch('src.capture.guesser._score_text', return_value=[(1, info, .99)]):
            result = _guess_from_texts(['春天的风轻轻吹过我们的脸庞'])
            self.assertEqual(result[0]['final_score'], .99)
            self.assertNotIn('support_count', result[0])
