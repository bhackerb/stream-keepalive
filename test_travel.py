"""Hermetic connected-display and exact local keepalive regression coverage."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch
import aiohttp
from aiohttp.test_utils import TestClient,TestServer
from api_server import StreamKeeperAPI
from display_probe import kscreen_outputs,xrandr_outputs,bind_displays,probe_displays
from regular_browser import RegularBrowserKeeper

D={'laptop':dict(x=4592,y=592,width=1970,height=1231,output='eDP-1'),'widescreen':dict(x=1152,y=383,width=3440,height=1440,output='DP-7')}
class ProbeTests(unittest.TestCase):
    def test_scaled_panel_uses_current_logical_geometry_not_docked_configuration(self):
        outputs=kscreen_outputs({'outputs':[{'name':'eDP-1','enabled':True,'connected':True,'pos':{'x':0,'y':0},'size':{'width':3840,'height':2400},'scale':1.95},{'name':'DP-7','connected':False,'enabled':False}]})
        displays,availability=bind_displays(D,outputs)
        self.assertEqual(displays['laptop']['x'],0);self.assertEqual(displays['laptop']['width'],1970)
        self.assertEqual(availability,{'laptop':'connected','widescreen':'disconnected'})
    def test_xrandr_signed_geometry_and_unknown_mapping(self):
        outputs=xrandr_outputs('eDP-1 connected primary 1920x1200+0+0 normal\nDP-7 disconnected normal\nHDMI-1 connected 3440x1440-3440+0 normal')
        self.assertEqual(outputs[2]['x'],-3440)
        displays,availability=bind_displays({'tv':dict(x=9,y=9,width=1,height=1)},outputs)
        self.assertEqual(displays,{});self.assertEqual(availability['tv'],'unknown')
    def test_rotated_kscreen_size_is_already_rotated(self):
        row=kscreen_outputs({'outputs':[{'name':'DP-4','connected':True,'enabled':True,'pos':{'x':0,'y':0},'size':{'width':1152,'height':2048},'rotation':8,'scale':1}]})[0]
        self.assertEqual((row['width'],row['height']),(1152,2048))
class TravelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.config={'api':{'api_key':'test-key'},'layout':{'displays':D},'session':{'state_file':str(Path(self.tmp.name)/'state')}}
        self.k=RegularBrowserKeeper(self.config);self.k.command=AsyncMock(return_value={'game':{}});self.k.force_reload=AsyncMock()
        self.patch=patch('regular_browser.probe_displays',AsyncMock(return_value={'state':'available','displays':{'laptop':dict(x=0,y=0,width=1970,height=1231)},'outputs':[],'availability':{'laptop':'connected','widescreen':'disconnected'}}));self.patch.start();self.addCleanup(self.patch.stop)
        self.k._add('game',{'url':'https://streamed.pk/watch/game/a/1','browser':'firefox'});self.k._browser_ready['firefox']=True
        self.k.local_keepalive={'enabled':True,'stream_id':'game','profile':'travel'}
        response=SimpleNamespace(status=200)
        context=Mock();context.__aenter__=AsyncMock(return_value=response);context.__aexit__=AsyncMock(return_value=None)
        self.k._client=SimpleNamespace(get=Mock(return_value=context))
    async def tick(self,now,state='NO_VIDEO',paused=False):
        with patch('regular_browser.time.time',return_value=now):
            self.k._frames[('game',0)]=(now,{'state':state,'width':1920,'height':1080,'paused':paused})
            await self.k._local_keepalive_tick()
    async def test_settings_persist_exact_selection_and_do_not_control_windows(self):
        self.k.local_keepalive={'enabled':False,'profile':'standard'}
        result=await self.k.settings_action({'viewing_mode':'laptop-only','keepalive':{'enabled':True,'profile':'travel','stream_id':'game'}})
        self.assertTrue(result['success']);self.k.command.assert_not_awaited()
        fresh=RegularBrowserKeeper(self.config);self.assertEqual(fresh.viewing_mode,'laptop-only');self.assertEqual(fresh.local_keepalive['stream_id'],'game')
        self.assertEqual(list(self.k._displays()),['laptop'])
    async def test_viewing_mode_can_change_after_selected_tab_closes(self):
        await self.k._event('firefox',{'event':'closed','stream_id':'game'})
        result=await self.k.settings_action({'viewing_mode':'laptop-only'})
        self.assertTrue(result['success']);self.assertIn('game',self.k.suppressed)
        await self.k._local_keepalive_tick();self.k.command.assert_not_awaited()
    async def test_auth_and_invalid_settings(self):
        async with TestClient(TestServer(StreamKeeperAPI(self.k,self.config).app))as client:
            self.assertEqual((await client.post('/settings',json={'viewing_mode':'laptop-only'})).status,401)
            self.assertEqual((await client.post('/settings',json={'keepalive':{'enabled':True,'stream_id':'other'}},headers={'Authorization':'Bearer test-key'})).status,400)
        with self.assertRaises(ValueError):await self.k.settings_action({'keepalive':{'enabled':True,'stream_id':'game','profile':'bad'}})
    async def test_unknown_detection_preserves_windows_and_rejects_offscreen_placements(self):
        self.patch.stop();self.patch=patch('regular_browser.probe_displays',AsyncMock(return_value={'state':'unknown','displays':{}}));self.patch.start()
        result=await self.k._place_once([dict(stream_id='game',display='widescreen',**D['widescreen'])])
        self.assertFalse(result['success']);self.assertIn('game',self.k.active_streams);self.k.command.assert_not_awaited()
    async def test_closed_paused_loading_stale_and_ownership_lost_never_reload(self):
        for state in ('PAUSED','LOADING','PLAYING'):
            await self.tick(1000,state);await self.tick(2000,state)
        await self.tick(3000,paused=True);await self.tick(4000,paused=True)
        self.k._ownership_lost.add('game');await self.tick(5000);await self.tick(6000)
        self.k.force_reload.assert_not_awaited();self.k.command.assert_not_awaited()
    async def test_local_care_does_not_discover_and_reloads_only_after_sustained_failure(self):
        self.k.discover=AsyncMock(side_effect=AssertionError('No catalog dependency'))
        await self.tick(1000);await self.tick(1119);self.k.force_reload.assert_not_awaited()
        await self.tick(1120);self.k.force_reload.assert_awaited_once_with('game')
        await self.tick(1200);self.assertEqual(self.k.force_reload.await_count,1)
        self.k.discover.assert_not_awaited();self.assertEqual(self.k._local_care['next_retry'],1720)
    async def test_offline_backoff_does_not_spend_reload_budget_or_switch(self):
        self.k._client.get.side_effect=aiohttp.ClientConnectionError()
        await self.tick(1000);await self.tick(1120)
        self.assertEqual(self.k._local_care['state'],'network-unavailable');self.assertEqual(self.k._local_care['next_retry'],1180)
        await self.tick(1130);self.assertEqual(self.k._client.get.call_count,1)
        await self.tick(1180);self.assertEqual(self.k._local_care['next_retry'],1300)
        self.k.force_reload.assert_not_awaited();self.assertEqual(self.k._local_care.get('attempts',0),0)
    async def test_healthy_budget_reset_requires_continuous_primary_progress(self):
        self.k._local_care={'attempts':3,'next_retry':9999}
        for now in range(1000,1310,10):
            self.k._progress[('game',0)]=now;await self.tick(now,'PLAYING')
        self.assertEqual(self.k._local_care['attempts'],0)
        self.k._local_care={'attempts':3};self.k._progress.clear();await self.tick(2000,'PLAYING');await self.tick(2400,'PLAYING')
        self.assertEqual(self.k._local_care['attempts'],3)
    async def test_buffering_after_long_offline_uses_fresh_evidence_and_prior_progress(self):
        self.k._progress[('game',0)]=900
        self.k._client.get.side_effect=aiohttp.ClientConnectionError()
        await self.tick(1000,'BUFFERING');await self.tick(1120,'BUFFERING')
        self.k._client.get.side_effect=None
        await self.tick(2000,'BUFFERING')
        self.k.force_reload.assert_awaited_once_with('game')
    async def test_actual_loading_events_after_long_outage_normalize_only_proven_document(self):
        self.k._progress[('game',0)]=900
        self.k._local_care['failure_since']=1000
        with patch('regular_browser.time.time',return_value=2000):
            await self.k._event('firefox',{'event':'health','stream_id':'game','frame_id':0,'health':{'state':'LOADING','current_time':10,'width':1920,'height':1080,'stall_seconds':1100,'paused':False}})
            self.assertEqual(self.k._frames[('game',0)][1]['state'],'BUFFERING')
            await self.k._local_keepalive_tick()
        self.k.force_reload.assert_awaited_once_with('game')
        self.k._progress.clear();self.k._frames.clear();self.k.force_reload.reset_mock()
        with patch('regular_browser.time.time',return_value=4000):
            await self.k._event('firefox',{'event':'health','stream_id':'game','frame_id':0,'health':{'state':'LOADING','current_time':0,'width':1920,'height':1080,'stall_seconds':1100,'paused':False}})
            self.assertEqual(self.k._frames[('game',0)][1]['state'],'LOADING')
            await self.k._local_keepalive_tick()
        self.k.force_reload.assert_not_awaited()
    async def test_explicit_shared_release_disables_care_and_closes_selected_stream(self):
        self.k.discover=AsyncMock(return_value={'live':[],'pending':[],'until_ms':9999999999999})
        self.k.command=AsyncMock(return_value=True)
        result=await self.k.set_session_layout({'mode':'manual','start':True,'group_id':'a'*32,'group_epoch':1,'display':'laptop','presets':{'laptop':1},'assignments':[],'release_ids':['game']})
        self.assertTrue(result['success']);self.assertFalse(self.k.local_keepalive['enabled']);self.assertNotIn('game',self.k.active_streams)
    async def test_network_request_race_user_close_wins(self):
        await self.tick(1000)
        async def close():
            self.k.suppressed.add('game');self.k.active_streams.pop('game');return SimpleNamespace(status=200)
        self.k._client.get.return_value.__aenter__.side_effect=close
        await self.tick(1120);self.k.force_reload.assert_not_awaited()
    async def test_playing_child_or_user_pause_during_network_check_cancels_reload(self):
        for change in ('child','pause'):
            self.k._frames.clear();self.k._local_care={}
            await self.tick(1000)
            async def changed():
                if change=='child':self.k._frames[('game',1)]=(1120,{'state':'PLAYING','width':640,'height':360})
                else:self.k._frames[('game',0)][1]['paused']=True
                return SimpleNamespace(status=200)
            self.k._client.get.return_value.__aenter__.side_effect=changed
            await self.tick(1120);self.k.force_reload.assert_not_awaited()
    async def test_enabled_care_fences_coordinator_reconcile_and_final_retirement(self):
        self.k.session={'id':'session'};self.k.active_streams['game'].session_id='session'
        self.k.manual_layout={'presets':{'widescreen':1},'assignments':[]}
        await self.k._reconcile({'live':[]})
        self.k.command.assert_not_awaited();self.assertIn('game',self.k.active_streams)

class UnknownProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_kwin_config_uses_owned_live_wayland_socket_with_stale_service_environment(self):
        import socket
        with tempfile.TemporaryDirectory()as directory:
            sock=socket.socket(socket.AF_UNIX);sock.bind(str(Path(directory)/'wayland-1'))
            try:
                process=SimpleNamespace(returncode=0,communicate=AsyncMock(return_value=(b'{"outputs":[]}',b'')))
                with patch.dict('display_probe.os.environ',{'XDG_RUNTIME_DIR':directory,'WAYLAND_DISPLAY':'wayland-0','DISPLAY':':0'},clear=True),patch('display_probe.shutil.which',return_value='/fake/kscreen-doctor'),patch('display_probe.asyncio.create_subprocess_exec',AsyncMock(return_value=process))as launch:
                    result=await probe_displays(D,'kwin')
                self.assertEqual(result['state'],'available');self.assertEqual(result['backend'],'kscreen')
                self.assertEqual(launch.call_args.kwargs['env']['WAYLAND_DISPLAY'],'wayland-1')
            finally:sock.close()
    async def test_missing_probe_is_unknown_not_disconnected(self):
        with patch('display_probe.shutil.which',return_value=None):result=await probe_displays(D)
        self.assertEqual(result['state'],'unknown');self.assertEqual(result['availability']['laptop'],'unknown')
