import unittest
from unittest.mock import AsyncMock
from manual_layout import validate_manual_layout,manual_placements
from test_session_planner import D
import test_regular_browser as fixtures
class GeometryTests(unittest.TestCase):
    def test_reserved_future_slot_does_not_resize_existing_stream(self):
        layout=validate_manual_layout({'presets':{'widescreen':6,'vertical':4},'assignments':[{'game_id':'now','display':'widescreen','slot':0},{'game_id':'later','display':'widescreen','slot':5}]},D)
        first=manual_placements(layout,D,{'now'});second=manual_placements(layout,D,{'now','later'})
        self.assertEqual(first[0],second[0]);self.assertEqual(second[1]['x'],3445)
    def test_conflicts_and_nonconfigured_slots_reject(self):
        base={'presets':{'tv':4},'assignments':[{'game_id':'a','display':'tv','slot':0}]}
        for extra in ({'game_id':'a','display':'tv','slot':1},{'game_id':'b','display':'tv','slot':0},{'game_id':'b','display':'tv','slot':4},{'game_id':'b','display':'vertical','slot':0}):
            with self.assertRaises(ValueError):validate_manual_layout({**base,'assignments':base['assignments']+[extra]},D)
class ManualTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.RegularTests.asyncSetUp
    asyncTearDown=fixtures.RegularTests.asyncTearDown
    plan=fixtures.RegularTests.plan
    async def test_save_draft_does_not_control_browser_or_change_auto_session(self):
        layout={'mode':'manual','presets':{'widescreen':6},'assignments':[{'game_id':'0','display':'widescreen','slot':0}]}
        prior=self.k.session.copy();result=await self.k.set_session_layout(layout)
        self.assertTrue(result['success']);self.assertEqual(self.k.session,prior);self.k.command.assert_not_awaited()
    async def test_future_game_starts_in_reserved_slot_and_auto_rank_cannot_move_it(self):
        self.k.manual_layout={'presets':{'widescreen':6},'assignments':[{'game_id':'0','display':'widescreen','slot':0},{'game_id':'1','display':'widescreen','slot':5}]}
        self.k._place=AsyncMock(return_value={'success':True})
        await self.k._reconcile(self.plan(1));before=self.k._place.await_args.args[0][0].copy()
        await self.k._reconcile(self.plan(2));after=self.k._place.await_args.args[0]
        self.assertEqual(before,after[0]);self.assertEqual(after[1]['stream_id'],'1')
        self.assertEqual(len(self.k.active_streams),2)
        self.assertTrue(all(c.args[1]=='open'for c in self.k.command.await_args_list))
    async def test_unknown_game_apply_fails_without_mutating_current_session(self):
        self.k.discover=AsyncMock(return_value=self.plan(1));prior=self.k.session.copy()
        with self.assertRaises(ValueError):await self.k.set_session_layout({'mode':'manual','start':True,'presets':{'tv':4},'assignments':[{'game_id':'unknown','display':'tv','slot':0}]})
        self.assertEqual(self.k.session,prior)
    async def test_reapply_does_not_reopen_closed_or_lost_games(self):
        self.k.suppressed.add('0');self.k._ownership_lost.add('1')
        self.k.discover=AsyncMock(return_value=self.plan(2))
        await self.k.set_session_layout({'mode':'manual','start':True,'presets':{'tv':4},'assignments':[{'game_id':'0','display':'tv','slot':0},{'game_id':'1','display':'tv','slot':1}]})
        self.k.command.assert_not_awaited();self.assertIn('0',self.k.suppressed);self.assertIn('1',self.k._ownership_lost)
    async def test_layout_roundtrip_preserves_windows_and_invalid_geometry_stops_tracking(self):
        from regular_browser import RegularBrowserKeeper
        self.k.manual_layout={'presets':{'widescreen':6},'assignments':[{'game_id':'0','display':'widescreen','slot':0}]}
        self.k.saved_layout=dict(self.k.manual_layout,spec=self.k.session['spec'])
        self.k._place=AsyncMock(return_value={'success':True})
        await self.k._reconcile(self.plan(1))
        fresh=RegularBrowserKeeper(self.config);fresh.command=AsyncMock();fresh._place=AsyncMock(return_value={'success':True})
        self.assertEqual(fresh.manual_layout,self.k.manual_layout);self.assertEqual(fresh.saved_layout,self.k.saved_layout)
        await fresh._reconcile(self.plan(1));fresh.command.assert_not_awaited();fresh._place.assert_awaited_once()
        changed=RegularBrowserKeeper({**self.config,'layout':{'displays':{'tv':D['tv']}}})
        self.assertIsNone(changed.session);self.assertIn('0',changed.active_streams)
    async def test_manual_to_auto_uses_fresh_catalog_and_browser_change_rejects(self):
        self.k.manual_layout={'presets':{'tv':4},'assignments':[]}
        self.k.discover=AsyncMock(return_value=self.plan(2))
        await self.k.set_session_layout({'mode':'auto'})
        self.assertIsNone(self.k.manual_layout);self.k.discover.assert_awaited_once();self.assertEqual(len(self.k.active_streams),2)
        with self.assertRaises(ValueError):await self.k.set_session_layout({'mode':'manual','start':True,'browser':'chromium','presets':{'tv':4},'assignments':[]})
    async def test_partial_apply_keeps_retryable_intent_and_reports_failure(self):
        self.k.discover=AsyncMock(return_value=self.plan(1));self.k._place=AsyncMock(side_effect=RuntimeError('fixture failure'))
        result=await self.k.set_session_layout({'mode':'manual','start':True,'presets':{'tv':4},'assignments':[{'game_id':'0','display':'tv','slot':0}]})
        self.assertTrue(result['errors']);self.assertIn('partially applied',result['errors'][0]);self.assertIsNotNone(self.k.manual_layout);self.assertIn('0',self.k.active_streams)
        self.k._place=AsyncMock(return_value={'success':True});await self.k._reconcile(self.plan(1));self.assertEqual(self.k.errors,[])
    async def test_layout_endpoint_requires_auth_and_invalid_draft_does_not_mutate(self):
        from aiohttp.test_utils import TestClient,TestServer
        from api_server import StreamKeeperAPI
        async with TestClient(TestServer(StreamKeeperAPI(self.k,self.config).app))as c:
            self.assertEqual((await c.post('/session-layout',json={'mode':'auto'})).status,401)
            self.assertEqual((await c.post('/session-layout',json={'mode':'manual','presets':{'tv':3}},headers={'Authorization':'Bearer test-key'})).status,400)
            self.k.command.assert_not_awaited()
        async with TestClient(TestServer(StreamKeeperAPI(self.k,{**self.config,'api':{'api_key':''}}).app))as c:
            self.assertEqual((await c.post('/session-layout',json={'mode':'auto'})).status,503)
    async def test_shared_release_closes_confirmed_source_without_user_suppression_and_fences_old_epoch(self):
        await self.k._reconcile(self.plan(2));self.k.discover=AsyncMock(return_value=self.plan(2));self.k._place=AsyncMock(return_value={'success':True})
        body={'mode':'manual','start':True,'group_id':'a'*32,'group_epoch':2,'presets':{'tv':4},'assignments':[{'game_id':'0','display':'tv','slot':0}],'release_ids':['1']}
        await self.k.set_session_layout(body);self.assertNotIn('1',self.k.active_streams);self.assertNotIn('1',self.k.suppressed)
        with self.assertRaises(ValueError):await self.k.set_session_layout({**body,'group_epoch':1})
        self.assertEqual(self.k.extra_status()['session']['group_epoch'],2)
    async def test_laptop_panel_adds_four_slots_between_wide_and_vertical(self):
        from display_layout import plan_layout
        displays={**D,'laptop':{'x':4592,'y':592,'width':1970,'height':1231}}
        layout=plan_layout(displays,'dock','grid',[str(i)for i in range(14)])
        self.assertEqual([p['display']for p in layout],['widescreen']*6+['laptop']*4+['vertical']*4)
    async def test_timed_out_release_keeps_new_desired_layout_and_late_close_is_not_user_suppression(self):
        await self.k._reconcile(self.plan(2));self.k.discover=AsyncMock(return_value=self.plan(2))
        self.k.command=AsyncMock(side_effect=TimeoutError('fixture'))
        body={'mode':'manual','start':True,'group_id':'b'*32,'group_epoch':1,'presets':{'tv':4},'assignments':[],'release_ids':['1']}
        with self.assertRaises(TimeoutError):await self.k.set_session_layout(body)
        self.assertEqual(self.k.manual_layout['assignments'],[]);self.assertIn('1',self.k._releasing)
        await self.k._event('firefox',{'event':'closed','stream_id':'1'})
        self.assertNotIn('1',self.k.active_streams);self.assertNotIn('1',self.k.suppressed)
        with self.assertRaises(ValueError):await self.k.set_session_layout(body)
    async def test_concurrent_same_epoch_has_one_winner_and_invalid_groups_return_value_error(self):
        import asyncio
        self.k.discover=AsyncMock(return_value=self.plan(0))
        body={'mode':'manual','start':True,'group_id':'c'*32,'group_epoch':1,'presets':{'tv':4},'assignments':[]}
        results=await asyncio.gather(self.k.set_session_layout(body),self.k.set_session_layout(body),return_exceptions=True)
        self.assertEqual(sum(isinstance(r,dict)for r in results),1);self.assertEqual(sum(isinstance(r,ValueError)for r in results),1)
        for group in ([],{},'bad'):
            with self.assertRaises(ValueError):await self.k.set_session_layout({**body,'group_id':group,'group_epoch':2})
    async def test_release_retry_with_new_epoch_converges_and_preserves_unrelated_suppression(self):
        await self.k._reconcile(self.plan(2));self.k.discover=AsyncMock(return_value=self.plan(2));self.k.suppressed.update(('other-sport-game','0'))
        self.k.command=AsyncMock(return_value=False)
        body={'mode':'manual','start':True,'group_id':'d'*32,'group_epoch':1,'reset_suppression':True,'presets':{'tv':4},'assignments':[],'release_ids':['1']}
        with self.assertRaises(ValueError):await self.k.set_session_layout(body)
        self.assertIn('1',self.k._releasing);self.assertIn('other-sport-game',self.k.suppressed);self.assertNotIn('0',self.k.suppressed)
        self.k.suppressed.add('0');self.k.command=AsyncMock(return_value=True)
        await self.k.set_session_layout({**body,'group_epoch':2})
        self.assertFalse(self.k._releasing);self.assertNotIn('1',self.k.suppressed);self.assertIn('0',self.k.suppressed)
    async def test_pregame_source_opens_early_only_with_real_stream_metadata(self):
        from unittest.mock import patch
        from test_session_planner import game
        g=game(20);g['date']=1000000+5*60000;g['sources']=[{'source':'golf','id':'test'}]
        async def catalog(path):
            if path=='matches/all':return[g]
            if path=='matches/live':return[]
            return[{'streamNo':1,'language':'English','viewers':1}]
        self.k._json=AsyncMock(side_effect=catalog)
        with patch('regular_browser.time.time',return_value=1000):
            plan=await self.k.discover({'sports':['hockey'],'preload_minutes':10})
        self.assertEqual(len(plan['live']),1);self.assertTrue(plan['live'][0]['prestart']);self.assertFalse(plan['live'][0]['live'])
        async def empty(path):return[g]if path=='matches/all'else[]
        self.k._json=AsyncMock(side_effect=empty)
        with patch('regular_browser.time.time',return_value=1000):
            plan=await self.k.discover({'sports':['hockey'],'preload_minutes':10})
        self.assertFalse(plan['live']);self.assertEqual(len(plan['pending']),1)
    async def test_explicit_recovery_audits_inventory_and_replaces_only_the_assigned_lost_game(self):
        self.k._ownership_lost.add('0');self.k.discover=AsyncMock(return_value=self.plan(1));self.k._place=AsyncMock(return_value={'success':True})
        body={'mode':'manual','start':True,'group_id':'e'*32,'group_epoch':1,'presets':{'tv':1},'assignments':[{'game_id':'0','display':'tv','slot':0}],'recover_ids':['0']}
        await self.k.set_session_layout(body)
        ops=self.k.command.await_args_list;self.assertEqual(ops[0].args[1],'inventory');self.assertEqual(ops[1].args[1],'open');self.assertTrue(ops[1].kwargs['allow_replacement']);self.assertNotIn('0',self.k._ownership_lost)
    async def test_tv_hardware_limit_blocks_quadrant_even_for_manual_requests(self):
        from display_layout import plan_layout
        displays={**D,'tv':{**D['tv'],'max_streams':2}}
        with self.assertRaises(ValueError):plan_layout(displays,'tv','grid',['a','b','c'])
        with self.assertRaises(ValueError):validate_manual_layout({'presets':{'tv':4},'assignments':[]},displays)
        self.assertEqual(len(plan_layout(displays,'tv','grid',['a','b'])),2)

    async def test_failed_recovery_audit_preserves_quarantine(self):
        self.k._ownership_lost.add('0');self.k.command=AsyncMock(return_value=False)
        body={'mode':'manual','start':True,'group_id':'f'*32,'group_epoch':1,'presets':{'tv':1},'assignments':[{'game_id':'0','display':'tv','slot':0}],'recover_ids':['0']}
        with self.assertRaises(ValueError):await self.k.set_session_layout(body)
        self.assertIn('0',self.k._ownership_lost);self.assertEqual(self.k.command.await_count,1)
