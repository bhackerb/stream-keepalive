"""Hermetic exact-stream play control and bounded unmute-pause repair."""
import asyncio
import time
import unittest
from unittest.mock import AsyncMock,patch
from aiohttp.test_utils import TestClient,TestServer
from api_server import StreamKeeperAPI
import test_audio_control as fixtures


class ResumeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.AudioTests.asyncSetUp(self)
        self.patches=[patch('regular_browser.PLAYBACK_CONFIRM_TIMEOUT',.08),patch('regular_browser.UNMUTE_OBSERVE_TIMEOUT',.06),patch('regular_browser.PLAYBACK_POLL_INTERVAL',.005)]
        for item in self.patches:item.start()
        self.tasks=[]
        self.k.command=AsyncMock(return_value=True)

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        for item in self.patches:item.stop()
        await fixtures.AudioTests.asyncTearDown(self)

    async def health(self,state='PAUSED',current=10,frames=100,frame_id=1,**extra):
        await self.k._event('firefox',{'event':'health','stream_id':'game','frame_id':frame_id,'health':{'state':state,'paused':state=='PAUSED','current_time':current,'decoded_frames':frames,'width':1280,'height':720,'muted':False,'volume':.3,**extra}})

    def later(self,callback):
        async def run():
            await asyncio.sleep(.01)
            await callback()
        task=asyncio.create_task(run());self.tasks.append(task)

    async def test_ack_only_and_playing_label_without_progress_fail_bounded_once(self):
        await self.health()
        for evidence in [None,lambda:self.health('PLAYING'),lambda:self.health('PLAYING',11,100)]:
            self.k.command.reset_mock()
            if evidence:self.later(evidence)
            started=asyncio.get_running_loop().time()
            result=await self.k.resume_playback('game')
            self.assertFalse(result['success']);self.assertIn('click Play',result['message'])
            self.assertLess(asyncio.get_running_loop().time()-started,.25)
            self.k.command.assert_awaited_once_with('firefox','play',stream_id='game')

    async def test_real_primary_progress_confirms_without_command_ack_and_preserves_audio(self):
        await self.health()
        before=self.k.active_streams['game']
        url=before.url
        async def stalled_command(*args,**kwargs):await asyncio.Future()
        self.k.command.side_effect=stalled_command
        self.later(lambda:self.health('PLAYING',11,120))
        result=await self.k.resume_playback('game')
        self.assertTrue(result['success']);self.assertTrue(result['playback_confirmed'])
        self.assertIs(before,self.k.active_streams['game']);self.assertEqual(before.url,url)
        self.assertFalse(before.health_monitor.stats['muted']);self.assertEqual(before.health_monitor.stats['volume'],.3)
        self.assertFalse(self.k._lock.locked())
        self.k.command.assert_awaited_once_with('firefox','play',stream_id='game')

    async def test_small_playing_child_stale_health_and_old_progress_cannot_confirm(self):
        await self.health()
        await self.health('PLAYING',1,10,frame_id=2,width=320,height=180)
        self.later(lambda:self.health('PLAYING',2,20,frame_id=2,width=320,height=180))
        result=await self.k.resume_playback('game');self.assertFalse(result['success'])
        self.k._frames[('game',1)]=(time.time()-20,{'state':'PLAYING','current_time':100,'width':1280,'height':720})
        self.k._frames.pop(('game',2))
        self.k._progress[('game',1)]=time.time()
        result=await self.k.resume_playback('game');self.assertFalse(result['success'])

    async def test_missing_initial_video_needs_two_fresh_progress_samples(self):
        async def evidence():
            await self.health('PLAYING',11,110)
            await asyncio.sleep(.015)
            await self.health('PLAYING',12,120)
        self.later(evidence)
        self.assertTrue((await self.k.resume_playback('game'))['success'])

    async def test_exact_ownership_parked_release_lost_and_disconnected_fail_without_play(self):
        self.assertFalse((await self.k.resume_playback('gam'))['success'])
        for blocked in [self.k._ownership_lost,self.k._releasing,self.k.suppressed]:
            blocked.add('game');self.assertFalse((await self.k.resume_playback('game'))['success']);blocked.clear()
        self.k.active_streams['game'].parked=True
        self.assertFalse((await self.k.resume_playback('game'))['success'])
        self.k.active_streams['game'].parked=False
        self.k.connections['firefox'].closed=True
        self.assertFalse((await self.k.resume_playback('game'))['success'])
        self.k.connections['firefox'].closed=False;self.k._browser_ready['firefox']=False
        self.assertFalse((await self.k.resume_playback('game'))['success'])
        self.k.command.assert_not_awaited()

    async def test_ownership_lost_during_confirmation_rejects_even_with_progress(self):
        await self.health()
        async def lost():
            self.k._ownership_lost.add('game');await self.health('PLAYING',11,120)
        self.later(lost)
        self.assertFalse((await self.k.resume_playback('game'))['success'])
        self.k.command.assert_awaited_once()

    def audio_commands(self,recover=True):
        async def command(browser,op,**args):
            self.assertTrue(self.k._lock.locked())
            if op=='audio':
                self.later(lambda:self.health('PAUSED'))
                return {'confirmed':True,'muted':False,'volume':.3,'media_count':1,'video_count':1}
            self.assertEqual(op,'play')
            if recover:self.later(lambda:self.health('PLAYING',11,120))
            return True
        self.k.command.side_effect=command

    async def test_unmute_new_pause_repairs_once_and_confirms_actual_progress(self):
        await self.health('PLAYING')
        self.audio_commands()
        result=await self.k.set_audio('game',False,.3)
        self.assertTrue(result['confirmed']);self.assertTrue(result['playback_confirmed'])
        self.assertEqual([c.args[1]for c in self.k.command.await_args_list],['audio','play'])

    async def test_progress_before_audio_ack_does_not_hide_pause_after_audio_applies(self):
        await self.health('PLAYING')
        async def command(browser,op,**args):
            if op=='audio':
                await self.health('PLAYING',11,110)
                self.later(lambda:self.health('PAUSED',11,110))
                return {'confirmed':True,'muted':False,'volume':.3,'media_count':1,'video_count':1}
            self.later(lambda:self.health('PLAYING',12,120))
            return True
        self.k.command.side_effect=command
        result=await self.k.set_audio('game',False,.3)
        self.assertTrue(result['playback_confirmed'])
        self.assertEqual([c.args[1]for c in self.k.command.await_args_list],['audio','play'])

    async def test_unmute_blocked_play_keeps_audio_confirmation_and_truthful_warning(self):
        await self.health('PLAYING')
        self.audio_commands(recover=False)
        result=await self.k.set_audio('game',False,.3)
        self.assertTrue(result['success']);self.assertTrue(result['confirmed']);self.assertFalse(result['playback_confirmed'])
        self.assertIn('click Play',result['playback_warning']);self.assertIn(result['playback_warning'],result['message'])
        self.assertEqual([c.args[1]for c in self.k.command.await_args_list],['audio','play'])

    async def test_intentionally_paused_stale_playing_and_muting_never_resume(self):
        for state,muted,stale in [('PAUSED',False,False),('PLAYING',True,False),('PLAYING',False,True)]:
            await self.health(state)
            if stale:
                timestamp,health=self.k._frames[('game',1)];self.k._frames[('game',1)]=(timestamp-20,health)
            self.k.command.reset_mock();self.k.command.return_value={'confirmed':True,'muted':muted,'volume':.3,'media_count':1,'video_count':1}
            result=await self.k.set_audio('game',muted,.3)
            self.assertTrue(result['confirmed']);self.assertNotIn('playback_warning',result)
            self.k.command.assert_awaited_once_with('firefox','audio',stream_id='game',muted=muted,volume=.3)

    async def test_unmute_missing_fresh_sample_warns_without_guessing_or_retry(self):
        await self.health('PLAYING')
        self.k.command.return_value={'confirmed':True,'muted':False,'volume':.3,'media_count':1,'video_count':1}
        result=await self.k.set_audio('game',False,.3)
        self.assertTrue(result['confirmed']);self.assertFalse(result['playback_confirmed']);self.assertIn('fresh playback',result['playback_warning'])
        self.k.command.assert_awaited_once()

    async def test_api_auth_strict_validation_and_conflict(self):
        async with TestClient(TestServer(StreamKeeperAPI(self.k,self.config).app))as client:
            headers={'Authorization':'Bearer test'}
            self.assertEqual((await client.post('/resume',json={'stream_id':'game'})).status,401)
            self.assertEqual((await client.post('/resume',json={'stream_id':'game'},headers={'Authorization':'Bearer wrong'})).status,401)
            for invalid in [[],{},None,{'stream_id':True},{'stream_id':' '},{'stream_id':'game','all':True},{'all':True}]:
                self.assertEqual((await client.post('/resume',json=invalid,headers=headers)).status,400)
            self.assertEqual((await client.post('/resume',data='invalid',headers=headers)).status,400)
            response=await client.post('/resume',json={'stream_id':'unknown'},headers=headers)
            self.assertEqual(response.status,409);self.assertFalse((await response.json())['success'])
            await self.health();self.later(lambda:self.health('PLAYING',11,120))
            response=await client.post('/resume',json={'stream_id':'game'},headers=headers)
            self.assertEqual(response.status,200);self.assertTrue((await response.json())['playback_confirmed'])
        for key in ['', 'YOUR_API_KEY']:
            async with TestClient(TestServer(StreamKeeperAPI(self.k,{**self.config,'api':{'api_key':key}}).app))as client:
                self.assertEqual((await client.post('/resume',json={'stream_id':'game'})).status,503)
