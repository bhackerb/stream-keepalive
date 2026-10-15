import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from regular_browser import RegularBrowserKeeper
from aiohttp.test_utils import TestClient,TestServer
from api_server import StreamKeeperAPI

class SourcePickerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
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
        self.k._json=AsyncMock(side_effect=[[{'id':'game','sources':[{'source':'golf','id':'provider-game'}]}],[{'streamNo':1,'language':'English','hd':True,'viewers':400},{'streamNo':2,'language':'German','viewers':100},{'streamNo':True},{'streamNo':-1}]])
        r=await self.k.player_sources('game')
        self.assertEqual(len(r['choices']),2);self.assertTrue(r['choices'][0]['current']);self.assertFalse(r['choices'][1]['current'])
        self.assertEqual([c.args[0]for c in self.k._json.call_args_list],['matches/all','stream/golf/provider-game'])
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

if __name__=='__main__':unittest.main()
