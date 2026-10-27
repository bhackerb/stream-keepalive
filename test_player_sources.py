import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch
from regular_browser import RegularBrowserKeeper
from aiohttp.test_utils import TestClient,TestServer
from api_server import StreamKeeperAPI

class SourcePickerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.display_patch=patch('regular_browser.probe_displays',AsyncMock(return_value={'state':'available','displays':{},'availability':{},'outputs':[]}))
        self.display_patch.start();self.addCleanup(self.display_patch.stop)
        self.tmp=tempfile.TemporaryDirectory()
        self.k=RegularBrowserKeeper({'api':{'api_key':'test'},'session':{'state_file':str(Path(self.tmp.name)/'state.json')}})
        self.k._add('game',{'url':'https://streamed.pk/watch/game/golf/1','label':'Game','browser':'firefox','session_id':'group'})
        self.old=self.k.active_streams['game']
        self.k.player_sources=AsyncMock(return_value={'choices':[{'url':'https://streamed.pk/watch/game/golf/2'}]})
        self.k.command=AsyncMock(side_effect=[{'game':{}},True,{},{}])
        self.k._place=AsyncMock(return_value={'success':True})
        self.k.layout_status={'placements':[dict(stream_id='game',display='tv',x=0,y=0,width=3840,height=2160)]}
    async def asyncTearDown(self):self.tmp.cleanup()
    async def test_switch_audits_before_close_and_drain_before_open_and_preserves_slot(self):
        result=await self.k.switch_source('game','https://streamed.pk/watch/game/golf/2')
        self.assertNotIn('❌',result)
        self.assertEqual([c.args[1]for c in self.k.command.call_args_list],['inventory','close','inventory','open'])
        self.assertEqual(self.k.active_streams['game'].url,'https://streamed.pk/watch/game/golf/2')
        self.assertEqual(self.k.active_streams['game'].session_id,'group')
        self.k._place.assert_awaited_once_with(self.k.layout_status['placements'])
        fresh=RegularBrowserKeeper(self.k.config)
        self.assertEqual(fresh.active_streams['game'].url,'https://streamed.pk/watch/game/golf/2')
        self.assertEqual(fresh._recovery['game']['attempts'],0)
    async def test_open_failure_retains_durable_source_and_forces_slot_retry(self):
        self.k._manual_signature=('old-slot',);self.k._layout_ids=('game',)
        self.k._open=AsyncMock(side_effect=ValueError('browser disconnected'))
        result=await self.k.switch_source('game','https://streamed.pk/watch/game/golf/2')
        self.assertIn('session retry',result)
        self.assertNotIn('game',self.k._releasing)
        self.assertEqual(self.k._manual_signature,());self.assertEqual(self.k._layout_ids,())
        fresh=RegularBrowserKeeper(self.k.config)
        self.assertEqual(fresh._recovery['game']['url'],'https://streamed.pk/watch/game/golf/2')
        self.assertFalse(fresh._recovery['game']['pending_release'])
        self.assertIn('retry pending',fresh._recovery['game']['message'])

    async def test_unlisted_cross_game_source_and_unknown_stream_do_not_touch_browser(self):
        for sid,url in [('game','https://streamed.pk/watch/other/golf/2'),('unknown','https://streamed.pk/watch/game/golf/2')]:
            self.assertIn('❌',await self.k.switch_source(sid,url))
        self.k.command.assert_not_awaited()
    async def test_failed_audit_never_closes(self):
        self.k.command=AsyncMock(return_value={})
        self.assertIn('❌',await self.k.switch_source('game','https://streamed.pk/watch/game/golf/2'))
        self.assertEqual([c.args[1]for c in self.k.command.call_args_list],['inventory'])
    async def test_unconfirmed_close_never_opens_and_saves_release_intent(self):
        self.k.command=AsyncMock(side_effect=[{'game':{}},False])
        self.assertIn('❌',await self.k.switch_source('game','https://streamed.pk/watch/game/golf/2'))
        self.assertIn('game',self.k._releasing)
        self.assertEqual(self.k._recovery['game']['url'],'https://streamed.pk/watch/game/golf/2')
        self.assertEqual([c.args[1]for c in self.k.command.call_args_list],['inventory','close'])
    async def test_drain_failure_never_opens(self):
        self.k.command=AsyncMock(side_effect=[{'game':{}},True,{'game':{}}])
        self.assertIn('❌',await self.k.switch_source('game','https://streamed.pk/watch/game/golf/2'))
        self.assertEqual([c.args[1]for c in self.k.command.call_args_list],['inventory','close','inventory'])
    async def test_user_close_during_lookup_never_opens(self):
        async def lookup(sid):
            self.k.suppressed.add(sid);self.k.active_streams.pop(sid,None)
            return {'choices':[{'url':'https://streamed.pk/watch/game/golf/2'}]}
        self.k.player_sources=lookup
        self.assertIn('❌',await self.k.switch_source('game','https://streamed.pk/watch/game/golf/2'))
        self.assertNotIn('close',[c.args[1]for c in self.k.command.call_args_list])
    async def test_lookup_only_game_catalog_sources_and_valid_numbers(self):
        self.k.player_sources=RegularBrowserKeeper.player_sources.__get__(self.k)
        current=self.k.active_streams['game'].url
        sources=[dict(url=current,provider='ppv',label='ESPN',language='English',hd=True,viewers=400),
                 dict(url='https://example.com/a',provider='ppv',label='ESPN Deportes',language='es',hd=False,viewers=None),
                 dict(url='https://example.org/b',provider='timstreams',label='Sky',language=None,hd=False,viewers=None)]
        self.k._catalog=AsyncMock(return_value={'matches':[{'id':'other','sources':[dict(sources[1],url='https://example.com/other')]},{'id':'game','category':'hockey','sources':sources}]})
        r=await self.k.player_sources('game')
        self.assertEqual([c['url']for c in r['choices']],[s['url']for s in sources])
        self.assertEqual([(c['source'],c['number'])for c in r['choices']],[('PPV.ST',1),('PPV.ST',2),('TimStreams',1)])
        self.assertTrue(r['choices'][0]['current']);self.assertFalse(r['choices'][1]['current'])
        self.assertEqual(r['choices'][1]['language'],'ESPN Deportes · es')
        self.k._catalog.assert_awaited_once_with(max_age=60)
    async def test_api_game_sources_and_switch_are_authenticated_and_scoped(self):
        api=StreamKeeperAPI(self.k,self.k.config)
        client=TestClient(TestServer(api.app));await client.start_server()
        try:
            self.assertEqual((await client.get('/player-sources?stream_id=game')).status,401)
            r=await client.get('/player-sources?stream_id=game',headers={'Authorization':'Bearer test'})
            self.assertEqual(r.status,200)
            self.k.switch_source=AsyncMock(return_value='ok')
            r=await client.post('/switch',json={'stream_id':'game','url':'https://streamed.pk/watch/game/golf/2'},headers={'Authorization':'Bearer test'})
            self.assertTrue((await r.json())['success']);self.k.switch_source.assert_awaited_once_with('game','https://streamed.pk/watch/game/golf/2')
        finally:await client.close()

    async def test_supplied_url_requires_capability_before_any_browser_action(self):
        url='https://olympicweb.me/game-live-stream/2/'
        self.assertIn('0.2.9',await self.k.switch_source('game',url,user_source=True))
        self.k.command.assert_not_awaited();self.k.player_sources.assert_not_awaited()
        self.assertIs(self.k.active_streams['game'],self.old)

    async def test_supplied_url_is_bound_to_game_preserves_slot_and_survives_restart_without_catalog(self):
        self.k._companion_info={'firefox':{'capabilities':['user-source-v1']}}
        url='https://falconstreams.st/live/mlb/match/game/123#/watch'
        self.k.command=AsyncMock(side_effect=[{'game':{}},True,{},{'url':url,'userSource':True}])
        result=await self.k.switch_source('game',url,user_source=True)
        self.assertNotIn('❌',result);self.k.player_sources.assert_not_awaited()
        self.assertEqual([c.args[1]for c in self.k.command.call_args_list],['inventory','close','inventory','open'])
        self.assertIs(self.k.command.call_args.kwargs['user_source'],True)
        self.assertTrue(self.k.active_streams['game'].user_source)
        self.assertEqual(self.k.active_streams['game'].site,'falconstreams.st')
        self.k._place.assert_awaited_once_with(self.k.layout_status['placements'])
        fresh=RegularBrowserKeeper(self.k.config)
        self.assertEqual(fresh.active_streams['game'].url,url);self.assertTrue(fresh.active_streams['game'].user_source)
        self.assertTrue(fresh._recovery['game']['user_source'])
        self.assertEqual(await fresh._opening_url({'id':'game'},0),url)
        fresh._catalog=AsyncMock(side_effect=AssertionError('No catalog for supplied URL'))
        self.assertIsNone(await fresh._recovery_sources({'id':'game','sources':[{'url':'https://example.com/other'}]},url,[]));fresh._catalog.assert_not_awaited()

    async def test_supplied_url_rejections_and_unconfirmed_release_never_open(self):
        self.k._companion_info={'firefox':{'capabilities':['user-source-v1']}}
        for sid,url,flag in [('missing','https://example.com/',True),('game','http://example.com/',True),('game','https://127.0.0.1/',True),('game','https://example.com/',1)]:
            self.assertIn('❌',await self.k.switch_source(sid,url,user_source=flag))
        self.k.command.assert_not_awaited()
        self.k.command=AsyncMock(side_effect=[{'game':{}},False])
        self.assertIn('❌',await self.k.switch_source('game','https://example.com/game',user_source=True))
        self.assertEqual([c.args[1]for c in self.k.command.call_args_list],['inventory','close'])
        self.assertIs(self.k.active_streams['game'],self.old)

    async def test_capability_metadata_is_independent_of_missing_catalog(self):
        self.k.player_sources=RegularBrowserKeeper.player_sources.__get__(self.k)
        self.k._catalog=AsyncMock(side_effect=TimeoutError())
        self.k._companion_info={'firefox':{'capabilities':['user-source-v1']}}
        metadata=await self.k.player_sources('game',include_catalog=False)
        self.assertTrue(metadata['user_source']['available']);self.k._catalog.assert_not_awaited()
        with self.assertRaises(TimeoutError):await self.k.player_sources('game')
        self.assertIn('timed out',await self.k.switch_source('game','https://streamed.pk/watch/game/golf/2'))
        self.k.command.assert_not_awaited()

    async def test_owned_companion_inventory_can_restore_explicit_url_without_broad_adoption(self):
        self.k.active_streams.clear()
        self.k._recovery['game']={'attempts':0,'tried':[],'url':'https://streamed.pk/watch/game/golf/1'}
        self.k._companion_info={'firefox':{'capabilities':['user-source-v1']}}
        url='https://olympicweb.me/game/2/'
        await self.k._event('firefox',{'event':'inventory','records':{'game':{'url':url,'userSource':True,'label':'Game'}}})
        self.assertEqual(self.k.active_streams['game'].url,url);self.assertTrue(self.k.active_streams['game'].user_source)
        self.assertTrue(self.k._recovery['game']['user_source'])
        self.assertEqual(self.k._recovery['game']['url'],url)
        await self.k._event('firefox',{'event':'inventory','records':{'unmarked':{'url':'https://other.example.com/'},'local':{'url':'https://127.0.0.1/','userSource':True}}})
        self.assertNotIn('unmarked',self.k.active_streams);self.assertNotIn('local',self.k.active_streams)

    async def test_inventory_cannot_overwrite_pending_source_selection(self):
        self.k._companion_info={'firefox':{'capabilities':['user-source-v1']}}
        selected='https://new.example.com/game'
        self.k._recovery['game']={'attempts':0,'tried':[],'url':selected,'user_source':True,'pending_release':True}
        await self.k._event('firefox',{'event':'inventory','records':{'game':{'url':'https://old.example.com/game','userSource':True}}})
        self.assertEqual(self.k._recovery['game']['url'],selected)

    async def test_pending_inventory_reports_the_actual_old_window_without_opening_the_requested_url(self):
        self.k.active_streams.clear();self.k._companion_info={'firefox':{'capabilities':['user-source-v1']}}
        requested='https://new.example.com/game';old='https://old.example.com/game'
        self.k._recovery['game']={'attempts':0,'tried':[],'url':requested,'user_source':True,'pending_release':True}
        await self.k._event('firefox',{'event':'inventory','records':{'game':{'url':old,'userSource':True}}})
        self.assertEqual(self.k.active_streams['game'].url,old)
        self.assertEqual(self.k._recovery['game']['url'],requested);self.assertTrue(self.k._recovery['game']['pending_release'])
        await self.k._open('game',requested,'Game','firefox')
        self.k.command.assert_not_awaited()  # Never duplicate a still-owned window.

    async def test_normal_catalog_timeout_http_contract_is_preserved(self):
        self.k.player_sources=RegularBrowserKeeper.player_sources.__get__(self.k);self.k._catalog=AsyncMock(side_effect=TimeoutError())
        api=StreamKeeperAPI(self.k,self.k.config);client=TestClient(TestServer(api.app));await client.start_server()
        try:
            r=await client.get('/player-sources?stream_id=game',headers={'Authorization':'Bearer test'})
            self.assertEqual(r.status,400);self.assertIn('Source lookup timed out',(await r.json())['error'])
            r=await client.get('/player-sources?stream_id=game&catalog=0',headers={'Authorization':'Bearer test'})
            self.assertEqual(r.status,200);self.assertIn('user_source',await r.json())
        finally:await client.close()

    async def test_another_browser_inventory_cannot_replace_selected_url(self):
        self.k._companion_info={'chromium':{'capabilities':['user-source-v1']}}
        selected='https://firefox.example.com/game'
        self.k._recovery['game']={'attempts':0,'tried':[],'url':selected,'user_source':True}
        await self.k._event('chromium',{'event':'inventory','records':{'game':{'url':'https://chromium.example.com/game','userSource':True}}})
        self.assertEqual(self.k._recovery['game']['url'],selected);self.assertEqual(self.k.active_streams['game'].browser,'firefox')

    async def test_failed_custom_open_retains_authorization_for_session_retry(self):
        self.k._companion_info={'firefox':{'capabilities':['user-source-v1']}}
        self.k._open=AsyncMock(side_effect=ValueError('Browser disconnected'))
        url='https://olympicweb.me/game/2/'
        self.assertIn('session retry',await self.k.switch_source('game',url,user_source=True))
        fresh=RegularBrowserKeeper(self.k.config);fresh._companion_info=self.k._companion_info
        self.assertNotIn('game',fresh.active_streams)
        selected=await fresh._opening_url({'id':'game'},0);self.assertEqual(selected,url)
        fresh._check_open_resources=AsyncMock();fresh.command=AsyncMock(return_value={'url':url,'userSource':True})
        await fresh._open('game',selected,'Game','firefox','group')
        self.assertTrue(fresh.command.call_args.kwargs['user_source']);self.assertTrue(fresh.active_streams['game'].user_source)

    async def test_saved_custom_retry_is_not_replaced_by_an_older_owned_receipt(self):
        self.k.active_streams.clear();self.k._companion_info={'firefox':{'capabilities':['user-source-v1']}}
        requested='https://new.example.com/game';old='https://old.example.com/game'
        self.k._recovery['game']={'attempts':0,'tried':[],'url':requested,'user_source':True,'pending_release':False}
        await self.k._event('firefox',{'event':'inventory','records':{'game':{'url':old,'userSource':True}}})
        self.assertEqual(self.k.active_streams['game'].url,old);self.assertEqual(self.k._recovery['game']['url'],requested)

    async def test_mismatched_open_receipt_is_not_reported_as_the_selected_source(self):
        self.k._companion_info={'firefox':{'capabilities':['user-source-v1']}}
        self.k.command=AsyncMock(side_effect=[{'game':{}},True,{},{'url':'https://old.example.com/game','userSource':True}])
        requested='https://new.example.com/game'
        self.assertIn('did not confirm',await self.k.switch_source('game',requested,user_source=True))
        self.assertNotIn('game',self.k.active_streams);self.assertEqual(self.k._recovery['game']['url'],requested)

    async def test_custom_flag_api_is_explicit_and_fast_metadata_does_not_fetch_catalog(self):
        api=StreamKeeperAPI(self.k,self.k.config);client=TestClient(TestServer(api.app));await client.start_server()
        headers={'Authorization':'Bearer test'}
        try:
            self.k.switch_source=AsyncMock(return_value='ok')
            for flag in ('true',1,None):
                r=await client.post('/switch',json={'stream_id':'game','url':'https://example.com/','user_source':flag},headers=headers)
                self.assertEqual(r.status,400)
            self.k.switch_source.assert_not_awaited()
            r=await client.post('/switch',json={'stream_id':'game','url':'https://example.com/','user_source':True},headers=headers)
            self.assertEqual(r.status,200);self.k.switch_source.assert_awaited_once_with('game','https://example.com/',user_source=True)
            await client.get('/player-sources?stream_id=game&catalog=0',headers=headers)
            self.k.player_sources.assert_awaited_once_with('game',include_catalog=False)
        finally:await client.close()

if __name__=='__main__':unittest.main()
