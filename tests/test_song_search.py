import unittest
from src.common.song_search import rank_songs


def song(sid, title, count=1, translation=''):
    return dict(id=sid,title=title,title_trans=translation,original_singer='Artist',count=count)


class SongSearchTests(unittest.TestCase):
    def test_formats_and_typos(self):
        cases = [('SparksFly','Sparks Fly'),('Spacks Fly','Sparks Fly'),
                 ('Troble is a friend','Trouble Is a Friend'),('Dance Money','Dance Monkey'),
                 ('We dont talk anymore',"We Don't Talk Anymore"),
                 ('I knew you were trouble.','I Knew You Were Trouble'),
                 ('說好的幸福呢','说好的幸福呢'),('恋爱困难少吕','恋爱困难少女'),
                 ('ｓａｙ　ｓｏ','Say So')]
        songs = [song(i,title) for i,(_,title) in enumerate(cases)]
        # 每个独立标题只对应一个歌曲，格式变体共用同一条。
        songs = list({s['title']:s for s in songs}.values())
        for query, expected in cases:
            with self.subTest(query=query):
                result = rank_songs(query,songs)
                self.assertEqual(result[0]['title'],expected)
                self.assertFalse(result[0]['needs_confirmation'])

    def test_exact_precedes_popularity_and_normalization(self):
        songs = [song(1,'Say Something',999),song(2,'Say So'),
                 song(3,'Lovefool'),song(4,'Love Fool',999)]
        self.assertEqual(rank_songs('say so',songs)[0]['id'],2)
        self.assertEqual(rank_songs('Lovefool',songs)[0]['id'],3)
        self.assertEqual(rank_songs('Love Fool',songs)[0]['id'],4)
        self.assertEqual(rank_songs('say',songs)[0]['id'],1)

    def test_ambiguous_and_no_unsafe_guess(self):
        self.assertTrue(rank_songs('stay',[song(1,'Stay'),song(2,'STAY',99)])[0]['needs_confirmation'])
        self.assertTrue(rank_songs('abcdefg',[song(1,'abcdefh'),song(2,'abcdefi')])[0]['needs_confirmation'])
        for q in ['wanan','', '%', '_', '爱你']:
            self.assertEqual(rank_songs(q,[song(1,'Bang Bang'),song(2,'爱我')]),[])

    def test_meaningful_symbol_exact_precedence(self):
        self.assertEqual(rank_songs('0+0',[song(1,'00',999),song(2,'0+0')])[0]['id'],2)
        self.assertEqual(rank_songs('说', [song(1,'Say So',translation='说吧')])[0]['id'],1)
