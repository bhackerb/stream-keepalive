"""Hermetic audio confirmation and legacy companion capability checks."""
import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from aiohttp.test_utils import TestClient,TestServer
from api_server import StreamKeeperAPI
from regular_browser import RegularBrowserKeeper

class AudioTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.config={'api':{'api_key':'test'},'session':{'state_file':str(Path(self.tmp.name)/'state.json')}}
        self.k=RegularBrowserKeeper(self.config)
        self.k._add('game',{'url':'https://streamed.pk/watch/game/golf/1','browser':'firefox'})
        self.k.connections['firefox']=SimpleNamespace(closed=False)
        self.k._browser_ready['firefox']=True
        self.k._companion_info['firefox']={'version':'0.2.0','capabilities':['audio-v1']}
        self.k.command=AsyncMock(return_value={'confirmed':True,'muted':False,'volume':0.3,'media_count':1,'video_count':1})
    async def asyncTearDown(self):self.tmp.cleanup()
    async def test_audio_requires_exact_owned_id_boolean_and_finite_volume(self):
        for sid,muted,volume in [('other',False,.3),('game',1,.3),('game',False,True),('game',False,-1),('game',False,math.nan),('game',False,math.inf)]:
            with self.assertRaises(ValueError):await self.k.set_audio(sid,muted,volume)
        self.k.command.assert_not_awaited()
    async def test_confirmed_result_updates_only_requested_owned_stream(self):
        result=await self.k.set_audio('game',False,.3)
        self.assertTrue(result['confirmed']);self.assertEqual(self.k.active_streams['game'].health_monitor.stats['volume'],.3)
        self.k.command.assert_awaited_once_with('firefox','audio',stream_id='game',muted=False,volume=.3)
    async def test_old_companion_blocks_audio_without_sending_unsupported_command(self):
        self.k._companion_info['firefox']={'version':'0.1.0'}
        with self.assertRaises(NotImplementedError):await self.k.set_audio('game',True)
        self.k.command.assert_not_awaited();self.assertFalse(self.k.extra_status()['audio_capabilities']['firefox']['available'])
    async def test_legacy_true_wrong_settings_and_zero_media_never_claim_success(self):
        for result in [True,{'confirmed':True,'muted':True,'media_count':1,'video_count':1},{'confirmed':True,'muted':False,'volume':.3,'media_count':0},{'confirmed':False}]:
            self.k.command.return_value=result
            with self.assertRaises(ValueError):await self.k.set_audio('game',False,.3)
        self.assertTrue(self.k.active_streams['game'].health_monitor.stats['muted'])
    async def test_api_auth_validation_legacy_capability_and_confirmation(self):
        async with TestClient(TestServer(StreamKeeperAPI(self.k,self.config).app))as c:
            body={'stream_id':'game','muted':False,'volume':.3}
            self.assertEqual((await c.post('/audio',json=body)).status,401)
            headers={'Authorization':'Bearer test'}
            for invalid in [[],{},dict(body,muted=1),dict(body,volume=None),dict(body,volume=2)]:
                self.assertEqual((await c.post('/audio',json=invalid,headers=headers)).status,400)
            r=await c.post('/audio',json=body,headers=headers);self.assertEqual(r.status,200);self.assertTrue((await r.json())['confirmed'])
            self.k._companion_info['firefox']={}
            r=await c.post('/audio',json=body,headers=headers);self.assertEqual(r.status,409);self.assertFalse((await r.json())['confirmed'])
    async def test_audio_requires_auth_even_when_dev_mode_allows_other_endpoints(self):
        async with TestClient(TestServer(StreamKeeperAPI(self.k,{**self.config,'api':{}}).app))as c:
            self.assertEqual((await c.post('/audio',json={'stream_id':'game','muted':True})).status,503)
