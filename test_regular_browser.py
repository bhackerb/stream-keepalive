import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch
from aiohttp.test_utils import TestClient,TestServer
from api_server import StreamKeeperAPI
from regular_browser import RegularBrowserKeeper,validate_url
from session_planner import build_plan
from native_host import read_message,write_message,launch_allowed
from test_session_planner import D,game
class NativeTests(unittest.TestCase):
    def test_framing_and_origin_validation(self):
        f=io.BytesIO();write_message(f,{'event':'hello','label':'⚽'});f.seek(0)
        self.assertEqual(read_message(f)['label'],'⚽')
        for data in (b'\x01',b'\xff'*4,b'\x05\0\0\0abc'):
            with self.assertRaises(ValueError):read_message(io.BytesIO(data))
        self.assertFalse(launch_allowed(['chrome-extension://untrusted/']))
        self.assertTrue(launch_allowed(['/tmp/com.bhackerb.streamkeeper.json','streamkeeper@bhackerb.local']))
    def test_url_allowlist(self):
        for url in ('http://streamed.pk/watch/a','https://streamed.pk.evil.example/a','https://user:pw@streamed.pk/watch/a','file:///tmp/a'):
            with self.assertRaises(ValueError):validate_url(url)
class RegularTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.config={'api':{'api_key':'test-key','enabled':True},'streams':{'max_streams':10},'layout':{'displays':D},'session':{'state_file':str(Path(self.tmp.name)/'state.json')}}
        self.k=RegularBrowserKeeper(self.config)
        self.k.command=AsyncMock(return_value=True)
        self.k.arrange_streams=AsyncMock(return_value={'success':True,'message':'fixture'})
        self.k.session={'id':'s','spec':{'sports':['hockey','baseball'],'display':'dock','browser':'firefox'},'until_ms':5000000}
    async def asyncTearDown(self):self.tmp.cleanup()
    def plan(self,n):
        games=[game(i)for i in range(n)]
        for g in games:g['sources']=[{'source':'admin','id':g['id']}]
        return build_plan(games,{g['id']for g in games},{},{'sports':['hockey','baseball']},D,1000000)
    async def test_later_join_is_idempotent_and_never_reloads_current_games(self):
        await self.k._reconcile(self.plan(5));await self.k._reconcile(self.plan(5));await self.k._reconcile(self.plan(7))
        self.assertEqual(len(self.k.active_streams),7)
        ops=[c.args[1]for c in self.k.command.await_args_list]
        self.assertEqual(ops,['open']*7)
        self.assertEqual(self.k.arrange_streams.await_count,2)
    async def test_viewer_fluctuation_does_not_move_windows(self):
        plan=self.plan(3);await self.k._reconcile(plan)
        plan['selected'].reverse();plan['live'].reverse();await self.k._reconcile(plan)
        self.assertEqual(self.k.arrange_streams.await_count,1)
    async def test_manual_close_suppresses_reopen_and_persists(self):
        await self.k._reconcile(self.plan(2));await self.k._event('firefox',{'event':'closed','stream_id':'0'})
        await self.k._reconcile(self.plan(2));self.assertNotIn('0',self.k.active_streams)
        fresh=RegularBrowserKeeper(self.config);self.assertIn('0',fresh.suppressed)
        self.assertEqual(self.k.state_path.stat().st_mode&0o777,0o600)
    async def test_catalog_disappearance_does_not_close_video(self):
        await self.k._reconcile(self.plan(2));await self.k._reconcile(self.plan(0))
        self.assertEqual(len(self.k.active_streams),2)
        self.assertTrue(all(c.args[1]=='open'for c in self.k.command.await_args_list))
    async def test_verified_end_retires_only_after_corroboration_and_budget(self):
        await self.k._reconcile(self.plan(1))
        with patch('regular_browser.time.time',return_value=100):
            self.k._frames[('0',0)]=(100,{'width':1920,'ended':True})
            await self.k._reconcile(self.plan(0));self.assertIn('0',self.k.active_streams)
        with patch('regular_browser.time.time',return_value=161):
            self.k._frames[('0',0)]=(161,{'width':1920,'ended':True})
            await self.k._reconcile(self.plan(0));self.assertNotIn('0',self.k.active_streams)
        self.assertEqual(self.k.command.await_args_list[-1].args[1],'close')
    async def test_missing_connection_is_reported_not_playback(self):
        self.k.command=AsyncMock(side_effect=ValueError('companion disconnected'))
        await self.k._reconcile(self.plan(1));self.assertFalse(self.k.is_watching);self.assertIn('companion disconnected',self.k.errors[0])
    async def test_unknown_browser_inventory_not_personal_tab_scan(self):
        await self.k._event('firefox',{'event':'inventory','records':{'owned':{'url':'https://streamed.pk/watch/game','label':'Game'}}})
        self.assertEqual(list(self.k.active_streams),['owned'])
        self.assertEqual(self.k.active_streams['owned'].health_monitor.stats['state'],'DISCONNECTED')
    async def test_bridge_and_session_require_auth_even_in_dev_mode(self):
        config={**self.config,'api':{'enabled':True,'api_key':''}}
        async with TestClient(TestServer(StreamKeeperAPI(self.k,config).app))as c:
            for path in ('/browser/bridge','/session'):
                response=await (c.get(path)if path.endswith('bridge')else c.post(path,json={'action':'status'}))
                self.assertEqual(response.status,503)
    async def test_tv_late_favorite_parks_lower_rank_without_reload(self):
        self.k.session['spec']['display']='tv'
        first=self.plan(6);first['capacity']=4
        await self.k._reconcile(first)
        later=self.plan(7);later['capacity']=4;later['live'][-1]['favorite']=True
        await self.k._reconcile(later)
        self.assertEqual(len(self.k.active_streams),5)
        self.assertEqual(sum(not s.parked for s in self.k.active_streams.values()),4)
        self.assertTrue(self.k.active_streams['3'].parked)
        self.assertTrue(any(c.args[1]=='park'for c in self.k.command.await_args_list))
        self.assertFalse(any(c.args[1]in('reload','close')for c in self.k.command.await_args_list))
    async def test_session_stop_preserves_manual_owned_stream(self):
        await self.k._reconcile(self.plan(2))
        self.k._add('manual',dict(url='https://streamed.pk/watch/manual',label='Manual',browser='firefox'))
        await self.k.session_action({'action':'stop'})
        self.assertEqual(list(self.k.active_streams),['manual'])
    async def test_inventory_refresh_does_not_rearrange_or_suppress_loss(self):
        await self.k._reconcile(self.plan(2))
        records={sid:dict(url=s.url,label=s.team)for sid,s in self.k.active_streams.items()}
        await self.k._event('firefox',{'event':'inventory','records':records})
        await self.k._reconcile(self.plan(2));self.assertEqual(self.k.arrange_streams.await_count,1)
        await self.k._event('firefox',{'event':'ownership_lost','stream_id':'0'})
        self.assertNotIn('0',self.k.suppressed)
    async def test_stop_all_attempts_other_owned_stream_after_stale_failure(self):
        await self.k._reconcile(self.plan(2));self.k.command=AsyncMock(side_effect=[ValueError('stale'),True])
        message=await self.k.stop();self.assertEqual(list(self.k.active_streams),['0']);self.assertIn('could not close',message);self.assertIsNone(self.k.session)
    async def test_restart_catalog_gap_keeps_streams_visible_and_unpaused(self):
        await self.k._reconcile(self.plan(2));fresh=RegularBrowserKeeper(self.config)
        fresh.command=AsyncMock(return_value=True);fresh.arrange_streams=AsyncMock(return_value={'success':True})
        await fresh._reconcile(self.plan(0))
        self.assertFalse(any(c.args[1]=='park'for c in fresh.command.await_args_list))
        self.assertEqual(len(fresh.active_streams),2)
    async def test_missing_inventory_quarantine_survives_daemon_restart(self):
        await self.k._reconcile(self.plan(1));await self.k._event('firefox',{'event':'inventory','records':{},'lost':['0']})
        fresh=RegularBrowserKeeper(self.config);fresh.command=AsyncMock(return_value=True);fresh.arrange_streams=AsyncMock(return_value={'success':True})
        await fresh._reconcile(self.plan(1));fresh.command.assert_not_awaited();self.assertIn('0',fresh._ownership_lost)
    async def test_cutoff_viewer_noise_does_not_pause_alternate_games(self):
        first=self.plan(5);first['capacity']=4
        for i,r in enumerate(first['live']):r['viewers']=100-i
        await self.k._reconcile(first)
        next_plan=self.plan(5);next_plan['capacity']=4
        for i,r in enumerate(next_plan['live']):r['viewers']=100-i
        next_plan['live'][-1]['viewers']=99
        await self.k._reconcile(next_plan)
        self.assertFalse(any(c.args[1]=='park'for c in self.k.command.await_args_list))
    async def test_late_baseball_gets_slot_in_full_hockey_layout(self):
        first=self.plan(4);first['capacity']=4
        for r in first['live']:r['viewers']=100
        await self.k._reconcile(first)
        later=self.plan(5);later['capacity']=4;later['live'][-1]['sport']='baseball'
        for r in later['live']:r['viewers']=100 if r['sport']=='hockey'else 1
        await self.k._reconcile(later)
        self.assertIn('4',self.k._visible_ids)
        self.assertEqual(sum(not s.parked for s in self.k.active_streams.values()),4)
    async def test_viewer_lookup_failure_preserves_existing_cutoff_rank(self):
        first=self.plan(5);first['capacity']=4
        for i,r in enumerate(first['live']):r['viewers']=200-i*10
        await self.k._reconcile(first)
        later=self.plan(5);later['capacity']=4
        for i,r in enumerate(later['live']):r['viewers']=None if i==3 else 200-i*10
        await self.k._reconcile(later)
        self.assertIn('3',self.k._visible_ids)
        self.assertTrue(later['live'][3]['viewer_stale'])
        self.assertFalse(any(c.args[1]=='park'for c in self.k.command.await_args_list))
    async def test_stop_and_same_games_restart_reapplies_layout(self):
        await self.k._reconcile(self.plan(2));await self.k.session_action({'action':'stop'})
        self.k.session={'id':'new','spec':{'sports':['hockey','baseball'],'display':'dock','browser':'firefox'},'until_ms':5000000};self.k.suppressed.clear()
        await self.k._reconcile(self.plan(2));self.assertEqual(self.k.arrange_streams.await_count,2)
    async def test_authenticated_status_exposes_connection_state(self):
        async with TestClient(TestServer(StreamKeeperAPI(self.k,self.config).app))as c:
            self.assertEqual((await c.get('/status')).status,401)
            response=await c.get('/status',headers={'Authorization':'Bearer test-key'})
            data=await response.json();self.assertEqual(data['control_mode'],'regular');self.assertFalse(data['browser_connections']['firefox'])
if __name__=='__main__':unittest.main()
