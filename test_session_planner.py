import unittest
from session_planner import build_plan, validate_spec
D = {'widescreen':dict(x=1152,y=383,width=3440,height=1440), 'vertical':dict(x=0,y=0,width=1152,height=2048), 'tv':dict(x=0,y=0,width=3840,height=2080)}
def game(i, start=1000000, sport='hockey', title=None):
    return dict(id=str(i),date=start,category=sport,title=title or 'Game '+str(i),sources=[])
class PlannerTests(unittest.TestCase):
    def test_favorites_before_viewers_and_unknown_last(self):
        p=build_plan([game(1,title='Ottawa Senators vs St. Louis Blues'),game(2),game(3)], {'1','2','3'}, {'1':1,'2':99}, {'favorites':['St. Louis Blues']},D,1000000)
        self.assertEqual([g['id'] for g in p['live']], ['1','2','3'])
    def test_later_mixed_sport_gets_slot_without_losing_existing_ids(self):
        games=[game(i) for i in range(5)]+[game(5,4600000),game(6,4600000,'baseball')]
        spec={'sports':['hockey','baseball']}
        a=build_plan(games,{str(i) for i in range(5)}, {},spec,D,1000000)
        b=build_plan(games,{str(i) for i in range(7)}, {},spec,D,4600000)
        self.assertEqual(len(a['pending']),2)
        self.assertEqual(len(b['placements']),7)
        self.assertTrue({r['id'] for r in a['selected']} <= {r['id'] for r in b['selected']})
        self.assertEqual(b['placements'][-1]['display'],'vertical')
    def test_channels_excluded_and_tv_overflow_visible(self):
        games=[game(i) for i in range(7)]+[game('network',0)]
        p=build_plan(games,{g['id'] for g in games},{},{'display':'tv'},D,1000000)
        self.assertEqual(len(p['selected']),1); self.assertEqual(len(p['overflow']),6)
    def test_horizon_fixed_and_future_not_opened_early(self):
        p=build_plan([game(1,2000000),game(2,90000000)],set(),{}, {},D,1000000,until_ms=3000000)
        self.assertEqual([g['id'] for g in p['pending']],['1']); self.assertEqual(p['selected'],[])
    def test_mixed_sports_reserves_baseball_even_with_lower_viewer_count(self):
        games=[game(i)for i in range(5)]+[game('baseball',sport='baseball')]
        p=build_plan(games,{g['id']for g in games},{**{str(i):100 for i in range(5)},'baseball':1},{'sports':['hockey','baseball'],'display':'dock'},D,1000000)
        self.assertEqual(len(p['selected']),6)
        self.assertIn('baseball',[g['id']for g in p['selected']])
    def test_invalid_types(self):
        for spec in ({'hours':True},{'sports':'hockey'},{'favorites':'Blues'},{'browser':'unknown'}):
            with self.assertRaises(ValueError):validate_spec(spec)
if __name__=='__main__': unittest.main()
