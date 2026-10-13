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
