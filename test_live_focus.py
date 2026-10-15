import sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
from aiohttp.test_utils import TestClient,TestServer
from regular_browser import RegularBrowserKeeper
from api_server import StreamKeeperAPI

class LiveFocusTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.tv=dict(x=0,y=0,width=3840,height=2160)
  self.k=RegularBrowserKeeper({'api':{'api_key':'test-key'},'layout':{'displays':{'tv':self.tv,'vertical':self.tv}},'session':{'state_file':str(Path(self.tmp.name)/'state')}})
  self.k.session={'id':'session'};self.p=dict(stream_id='game',display='tv',**self.tv);self.other=dict(stream_id='other',display='vertical',**self.tv)
  self.k.plan={'placements':[self.p,self.other]};self.k.layout_status={'success':True,'placements':[self.p,self.other]}
  self.k.active_streams={'game':SimpleNamespace(page='page',browser='firefox',parked=False,session_id='session')}
  self.k.command=AsyncMock(return_value={'game':{'fill':True}});self.k.command_for=AsyncMock(return_value=True);self.k._browser_ready['firefox']=True
  self.identify=AsyncMock(return_value=42);self.matches=Mock(return_value=True);self.move=Mock()
  self.patch=patch.dict(sys.modules,{'x11_windows':SimpleNamespace(window_for_page=self.identify,window_matches=self.matches,wait_window_matches=self.matches,move_window=self.move)});self.patch.start()
 async def asyncTearDown(self):self.patch.stop();self.tmp.cleanup()
 async def test_noop_when_fullscreen_and_repairs_only_actual_tv_drift(self):
  await self.k._maintain_tv_focus();self.move.assert_not_called()
  self.matches.side_effect=[False,True];await self.k._maintain_tv_focus();self.move.assert_called_once_with(42,self.p,fullscreen=True)
 async def test_escape_half_slot_and_pending_release_never_repaired(self):
  self.k.command.return_value={'game':{'fill':False}};await self.k._maintain_tv_focus();self.identify.assert_not_called()
  self.k.command.return_value={'game':{'fill':True}};self.p['width']=1920;await self.k._maintain_tv_focus();self.identify.assert_not_called()
  self.p['width']=3840;self.k._releasing.add('game');await self.k._maintain_tv_focus();self.identify.assert_not_called()
 async def test_restore_preserves_other_displays_and_never_opens_or_closes(self):
  self.k.layout_status={'success':False,'message':'Earlier placement failed'}
  result=await self.k.restore_live_layout('tv');self.assertTrue(result['success']);self.assertIn(self.other,self.k.layout_status['placements'])
  self.k.command_for.assert_awaited_once_with('game','fill',enabled=True);self.move.assert_called_once_with(42,self.p,fullscreen=True)
 async def test_restore_rejects_missing_session_or_released_player(self):
  self.k._ownership_lost.add('game')
  with self.assertRaises(ValueError):await self.k.restore_live_layout('tv')
  self.k.command_for.assert_not_called();self.k.session=None
  with self.assertRaises(ValueError):await self.k.restore_live_layout('tv')
 async def test_restore_cannot_report_success_when_fullscreen_geometry_rejected(self):
  self.matches.return_value=False
  self.assertFalse((await self.k.restore_live_layout('tv'))['success'])
  self.assertIn(self.other,self.k.layout_status['placements'])
 async def test_restore_requires_fill_acceptance_before_moving(self):
  self.k.command_for.return_value=False
  with self.assertRaises(ValueError):await self.k.restore_live_layout('tv')
  self.move.assert_not_called()
 async def test_restore_endpoint_requires_auth_and_valid_display(self):
  async with TestClient(TestServer(StreamKeeperAPI(self.k,self.k.config).app))as c:
   self.assertEqual((await c.post('/restore-layout',json={'display':'tv'})).status,401)
   h={'Authorization':'Bearer test-key'}
   self.assertEqual((await c.post('/restore-layout',json={'display':'nope'},headers=h)).status,409)
   self.assertEqual((await c.post('/restore-layout',json={'display':'tv'},headers=h)).status,200)
   self.identify.side_effect=RuntimeError('Window disappeared')
   self.assertEqual((await c.post('/restore-layout',json={'display':'tv'},headers=h)).status,409)

class WindowSettleTests(unittest.TestCase):
 def load(self):
  import importlib.util
  spec=importlib.util.spec_from_file_location('focus_x11_fixture',Path(__file__).with_name('x11_windows.py'));m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
 def test_window_manager_can_settle_before_geometry_is_confirmed(self):
  m=self.load()
  with patch.object(m,'window_matches',side_effect=[False,False,True])as check,patch.object(m.time,'sleep'):
   self.assertTrue(m.wait_window_matches(42,{},True));self.assertEqual(check.call_count,3)
 def test_unconfirmed_geometry_has_a_bounded_wait(self):
  m=self.load()
  with patch.object(m,'window_matches',return_value=False)as check,patch.object(m.time,'sleep'):
   self.assertFalse(m.wait_window_matches(42,{},True));self.assertEqual(check.call_count,50)
