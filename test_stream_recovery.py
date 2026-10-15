import unittest
from unittest.mock import AsyncMock,patch
from regular_browser import RegularBrowserKeeper
import test_regular_browser as fixtures

class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.RegularTests.asyncSetUp
    asyncTearDown=fixtures.RegularTests.asyncTearDown
    plan=fixtures.RegularTests.plan

    async def setup_stream(self):
        self.p=self.plan(1);await self.k._reconcile(self.p)
        self.k._browser_ready['firefox']=True;self.k._visible_ids=('0',)
        self.k.command.reset_mock()
        async def commands(browser,op,**args):
            if op=='inventory':return {'0':{}}
            return True
        self.k.command=AsyncMock(side_effect=commands)

    async def tick(self,now,state='NO_VIDEO'):
        self.k._frames[('0',0)]=(now,{'state':state})
        with patch('regular_browser.time.time',return_value=now):await self.k._recover_no_video(self.p)

    async def test_wait_refresh_switch_order_budget_and_restart(self):
        await self.setup_stream();await self.tick(1000);await self.tick(1089)
        self.k.command.assert_not_awaited();await self.tick(1090)
        self.assertEqual([c.args[1]for c in self.k.command.await_args_list],['inventory','reload'])
        self.k._recovery_sources=AsyncMock(return_value='https://streamed.pk/watch/0/golf/2')
        async def commands(browser,op,**args):
            if op=='inventory':return {'0':{}}if '0'in self.k.active_streams else {}
            if op=='close':await self.k._event('firefox',{'event':'closed','stream_id':'0'})
            return True
        self.k.command=AsyncMock(side_effect=commands)
        await self.tick(1180)
        self.assertEqual([c.args[1]for c in self.k.command.await_args_list],['inventory','close','inventory','open'])
        self.assertNotIn('0',self.k.suppressed);self.assertEqual(self.k.active_streams['0'].url,'https://streamed.pk/watch/0/golf/2')
        self.k._recovery_sources.return_value='https://streamed.pk/watch/0/admin/3';await self.tick(1270)
        self.assertEqual(self.k._recovery['0']['attempts'],3)
        self.k.command.reset_mock();await self.tick(1400);self.k.command.assert_not_awaited()
        fresh=RegularBrowserKeeper(self.config);self.assertEqual(fresh._recovery['0']['attempts'],3);self.assertNotIn('since',fresh._recovery['0'])

    async def test_playing_child_paused_loading_stale_and_disconnected_never_mutate(self):
        await self.setup_stream();self.k._recovery['0']={'attempts':0,'tried':[],'since':1}
        for state in ['PLAYING','PAUSED','LOADING','ERROR','STALE','DISCONNECTED']:
            await self.tick(1000,state);self.k.command.assert_not_awaited()
        self.k._recovery['0']['since']=1;self.k._frames[('0',1)]=(1000,{'state':'PLAYING'})
        await self.tick(1000);self.k.command.assert_not_awaited()
        self.k._frames.clear();self.k._browser_ready['firefox']=False;await self.tick(1200);self.k.command.assert_not_awaited()

    async def test_closed_parked_lost_releasing_and_non_session_are_protected(self):
        await self.setup_stream()
        for blocked in [self.k.suppressed,self.k._ownership_lost,self.k._releasing]:
            self.k._recovery['0']={'attempts':0,'tried':[],'since':1};blocked.add('0');await self.tick(1000);blocked.clear()
        self.k.active_streams['0'].parked=True;await self.tick(1000)
        self.k.active_streams['0'].parked=False;self.k.active_streams['0'].session_id=None;await self.tick(1000)
        self.k.command.assert_not_awaited()

    async def test_failed_inventory_and_failed_close_block_replacement(self):
        await self.setup_stream();self.k._recovery['0']={'attempts':1,'tried':[],'since':1}
        self.k.command=AsyncMock(return_value=False);await self.tick(1000)
        self.assertEqual(self.k.command.await_count,1);self.assertEqual(self.k._recovery['0']['attempts'],1)
        self.k._recovery_sources=AsyncMock(return_value='https://streamed.pk/watch/0/golf/2')
        self.k.command=AsyncMock(side_effect=[{'0':{}},False]);await self.tick(1200)
        self.assertIn('0',self.k._releasing);self.assertEqual(self.k._recovery['0']['attempts'],2)
        self.assertFalse(any(c.args[1]=='open'for c in self.k.command.await_args_list))

    async def test_playback_or_user_close_during_catalog_lookup_cancels_switch(self):
        await self.setup_stream();self.k._recovery['0']={'attempts':1,'tried':[],'since':1}
        async def sources(*args):
            self.k._frames[('0',1)]=(1000,{'state':'PLAYING'});return 'https://streamed.pk/watch/0/golf/2'
        self.k._recovery_sources=AsyncMock(side_effect=sources);await self.tick(1000)
        self.assertEqual([c.args[1]for c in self.k.command.await_args_list],['inventory'])
        self.k._frames.clear();self.k.command.reset_mock();self.k._recovery['0']['since']=1
        async def closed(*args):
            await self.k._event('firefox',{'event':'closed','stream_id':'0'});return 'https://streamed.pk/watch/0/golf/2'
        self.k._recovery_sources.side_effect=closed;await self.tick(1200)
        self.assertIn('0',self.k.suppressed);self.assertEqual(self.k.command.await_count,1)

    async def test_source_catalog_filters_tried_invalid_and_prefers_english(self):
        row={'id':'0','sources':[{'source':'golf','id':'a'}]}
        self.k._json=AsyncMock(return_value=[{'streamNo':1,'language':'English'},{'streamNo':2,'language':'French','hd':True},{'streamNo':3,'language':'English'},{'streamNo':True}])
        result=await self.k._recovery_sources(row,'https://streamed.pk/watch/0/golf/1',[])
        self.assertEqual(result,'https://streamed.pk/watch/0/golf/3')
        self.k._json.assert_awaited_once_with('stream/golf/a')

    async def test_health_transition_between_polls_resets_failure_timer(self):
        await self.setup_stream();await self.tick(1000)
        with patch('regular_browser.time.time',return_value=1040):
            await self.k._event('firefox',{'event':'health','stream_id':'0','frame_id':0,'health':{'state':'LOADING'}})
        await self.tick(1090);self.k.command.assert_not_awaited();await self.tick(1179);self.k.command.assert_not_awaited()
        await self.tick(1180);self.assertEqual(self.k._recovery['0']['attempts'],1)

    async def test_stale_child_evidence_blocks_recovery(self):
        await self.setup_stream();self.k._recovery['0']={'attempts':0,'tried':[],'since':1}
        self.k._frames[('0',1)]=(900,{'state':'PLAYING'})
        await self.tick(1000);self.k.command.assert_not_awaited()

    async def test_unconfirmed_close_restart_is_resolved_by_ordered_audit(self):
        await self.setup_stream();self.k._recovery['0']={'attempts':2,'tried':[],'pending_release':True};self.k._releasing.add('0');self.k._save()
        self.k=RegularBrowserKeeper(self.config);self.k._browser_ready['firefox']=True
        self.k.command=AsyncMock(return_value={'0':{}})
        await self.tick(1000);self.assertNotIn('0',self.k._releasing);self.assertFalse(self.k._recovery['0']['pending_release'])
        self.assertEqual([c.args[1]for c in self.k.command.await_args_list],['inventory'])

    async def test_missing_pre_audit_and_existing_refresh_cooldown_block_mutation(self):
        await self.setup_stream();self.k._recovery['0']={'attempts':0,'tried':[],'since':1}
        self.k.command=AsyncMock(return_value={});await self.tick(1000);self.assertEqual(self.k._recovery['0']['attempts'],0)
        self.k.command=AsyncMock(return_value={'0':{}});self.k._reload_times['0']=1100;await self.tick(1200)
        self.assertEqual([c.args[1]for c in self.k.command.await_args_list],['inventory'])

    async def test_manual_close_during_native_close_does_not_reopen(self):
        await self.setup_stream();self.k._recovery['0']={'attempts':1,'tried':[],'since':1}
        self.k._recovery_sources=AsyncMock(return_value='https://streamed.pk/watch/0/golf/2')
        async def commands(browser,op,**args):
            if op=='inventory':return {'0':{}}if '0'in self.k.active_streams else {}
            if op=='close':
                await self.k._event('firefox',{'event':'closed','stream_id':'0'})
                raise ValueError('Stream is not owned') # signed companion rejects an already user-closed tab
            return True
        self.k.command=AsyncMock(side_effect=commands);await self.tick(1000)
        self.assertFalse(any(c.args[1]=='open'for c in self.k.command.await_args_list))
        self.assertIn('0',self.k._ownership_lost)
        await self.k._reconcile(self.p)
        self.assertFalse(any(c.args[1]=='open'for c in self.k.command.await_args_list))

    async def test_bad_recovery_state_is_ignored(self):
        import json
        self.k.state_path.write_text(json.dumps({'recovery':[]}))
        fresh=RegularBrowserKeeper(self.config);self.assertEqual(fresh._recovery,{})

    async def test_only_one_game_refreshes_per_tick(self):
        self.p=self.plan(2);await self.k._reconcile(self.p);self.k._browser_ready['firefox']=True;self.k._visible_ids=('0','1')
        self.k.command=AsyncMock(return_value={'0':{},'1':{}})
        for sid in ['0','1']:
            self.k._recovery[sid]={'attempts':0,'tried':[],'since':1};self.k._frames[(sid,0)]=(1000,{'state':'NO_VIDEO'})
        with patch('regular_browser.time.time',return_value=1000):await self.k._recover_no_video(self.p)
        self.assertEqual(sum(c.args[1]=='reload'for c in self.k.command.await_args_list),1)

    async def test_malformed_persisted_fields_do_not_break_save_or_reconcile(self):
        import json
        for bad in [{'last_action':'bad'},{'created_at':[]},{'url':42},{'url':'https://evil.example/watch/0'},{'pending_release':1}]:
            self.k.state_path.write_text(json.dumps({'recovery':{'0':{'attempts':1,'tried':[],**bad}}}))
            fresh=RegularBrowserKeeper(self.config);self.assertEqual(fresh._recovery,{});fresh._save()

    async def test_persisted_alternate_is_used_after_restart(self):
        self.k._recovery['0']={'attempts':2,'tried':[],'url':'https://streamed.pk/watch/0/golf/2'};self.k._save()
        fresh=RegularBrowserKeeper(self.config);fresh.session=self.k.session;fresh.command=AsyncMock(return_value=True);fresh.arrange_streams=AsyncMock(return_value={'success':True})
        await fresh._reconcile(self.plan(1))
        self.assertEqual(fresh.active_streams['0'].url,'https://streamed.pk/watch/0/golf/2')

    async def test_timed_out_close_then_absence_reopens_saved_alternate(self):
        await self.setup_stream();self.k._recovery['0']={'attempts':1,'tried':[],'since':1}
        self.k._recovery_sources=AsyncMock(return_value='https://streamed.pk/watch/0/golf/2')
        self.k.command=AsyncMock(side_effect=[{'0':{}},TimeoutError('close delayed')]);await self.tick(1000)
        self.assertTrue(self.k._recovery['0']['pending_release']);self.assertEqual(self.k._recovery['0']['url'],'https://streamed.pk/watch/0/golf/2')
        async def commands(browser,op,**args):
            if op=='inventory':
                await self.k._event('firefox',{'event':'inventory','records':{}});return {}
            return True
        self.k.command=AsyncMock(side_effect=commands);await self.tick(1200)
        self.assertNotIn('0',self.k._releasing);self.assertEqual(self.k.active_streams['0'].url,'https://streamed.pk/watch/0/golf/2')

class BufferingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.RegularTests.asyncSetUp
    asyncTearDown=fixtures.RegularTests.asyncTearDown
    plan=fixtures.RegularTests.plan
    setup_stream=RecoveryTests.setup_stream
    async def health(self,now,current,state='LOADING',frames=20,stall=0,**extra):
        with patch('regular_browser.time.time',return_value=now):
            await self.k._event('firefox',{'event':'health','stream_id':'0','frame_id':0,'health':{'state':state,'current_time':current,'width':1280,'height':720,'decoded_frames':frames,'stall_seconds':stall,**extra}})
    async def test_loading_needs_observed_prior_video_progress_before_buffering(self):
        await self.setup_stream()
        await self.health(1000,10,stall=100)
        self.assertEqual(self.k.active_streams['0'].health_monitor.stats['state'],'LOADING')
        await self.health(1003,11,'PLAYING',frames=40)
        for now in range(1006,1022,3):await self.health(now,11,frames=40,stall=now-1003)
        self.assertEqual(self.k.active_streams['0'].health_monitor.stats['state'],'BUFFERING')
        self.k.command.assert_not_awaited()
    async def test_buffering_recovers_only_after_sustained_fresh_proven_stall(self):
        await self.setup_stream();await self.health(1000,10,'PLAYING');await self.health(1003,11,'PLAYING',frames=40)
        for now in range(1006,1111,3):
            await self.health(now,11,frames=40,stall=now-1003)
            with patch('regular_browser.time.time',return_value=now):await self.k._recover_no_video(self.p)
        self.assertEqual([c.args[1]for c in self.k.command.await_args_list],['inventory','reload'])
        self.assertEqual(self.k._recovery['0']['attempts'],1)
        self.assertNotIn(('0',0),self.k._progress)
    async def test_paused_ended_old_progress_and_decode_freeze_do_not_recover(self):
        for extra in [{'paused':True},{'ended':True}]:
            self.k._frames.clear();self.k._progress.clear()
            await self.setup_stream();await self.health(1000,10,'PLAYING');await self.health(1003,11,'PLAYING',frames=40)
            await self.health(1020,11,frames=40,stall=17,**extra)
            self.assertEqual(self.k.active_streams['0'].health_monitor.stats['state'],'LOADING')
        self.k._frames.clear();self.k._progress.clear()
        await self.setup_stream();await self.health(1000,10,'PLAYING');await self.health(1003,11,'PLAYING',frames=20)
        self.assertNotIn(('0',0),self.k._progress)
        await self.health(1020,11,stall=17);self.assertEqual(self.k.active_streams['0'].health_monitor.stats['state'],'LOADING')
        self.k._progress[('0',0)]=700;await self.health(1023,11,stall=320)
        self.assertEqual(self.k.active_streams['0'].health_monitor.stats['state'],'LOADING');self.k.command.assert_not_awaited()

    async def seed_buffering(self):
        await self.setup_stream();self.k._frames.clear();self.k._progress.clear();self.k._recovery.clear();self.k._reload_times.clear()
        await self.health(1000,10,'PLAYING');await self.health(1003,11,'PLAYING',frames=40)
        await self.health(1018,11,frames=40,stall=15)
        with patch('regular_browser.time.time',return_value=1018):await self.k._recover_no_video(self.p)
        self.assertEqual(self.k._recovery['0']['since'],1018)

    async def sustain_until(self,start,end,checkpoints=()):
        for now in sorted(set(range(start,end+1,3))|set(checkpoints)|{end}):
            await self.health(now,11,frames=40,stall=now-1003)
            if now in checkpoints:
                with patch('regular_browser.time.time',return_value=now):await self.k._recover_no_video(self.p)
                if now<end:self.k.command.assert_not_awaited()

    async def test_exact_sustained_buffering_boundaries_60_85_89_90_seconds(self):
        await self.seed_buffering()
        await self.sustain_until(1021,1107,(1078,1103,1107))
        self.k.command.assert_not_awaited()
        await self.health(1108,11,frames=40,stall=105)
        with patch('regular_browser.time.time',return_value=1108):await self.k._recover_no_video(self.p)
        self.assertEqual([c.args[1]for c in self.k.command.await_args_list],['inventory','reload'])

    async def test_loading_and_playing_each_reset_the_sustained_buffering_timer(self):
        for state in ('LOADING','PLAYING'):
            with self.subTest(state=state):
                await self.seed_buffering();await self.sustain_until(1021,1075)
                await self.health(1078,11,state,frames=40,stall=0)
                self.assertNotIn('since',self.k._recovery['0'])
                await self.health(1081,11,frames=40,stall=78)
                with patch('regular_browser.time.time',return_value=1081):await self.k._recover_no_video(self.p)
                self.assertEqual(self.k._recovery['0']['since'],1081)
                await self.sustain_until(1084,1170,(1108,1141,1166,1170))
                self.k.command.assert_not_awaited()
                await self.health(1171,11,frames=40,stall=168)
                with patch('regular_browser.time.time',return_value=1171):await self.k._recover_no_video(self.p)
                self.assertEqual([c.args[1]for c in self.k.command.await_args_list],['inventory','reload'])

    async def test_progress_older_than_300_seconds_blocks_alternate_even_after_90_seconds(self):
        await self.seed_buffering()
        await self.sustain_until(1021,1214)
        self.k._recovery['0'].update(attempts=1,since=1214,last_action=1100)
        self.k._recovery_sources=AsyncMock(return_value='https://streamed.pk/watch/0/golf/2')
        # At exactly five minutes, this evidence remains BUFFERING but has only
        # 89 seconds of the current failure. One second later progress expires.
        await self.sustain_until(1217,1303)
        self.assertEqual(self.k._recovery['0']['since'],1214)
        self.assertEqual(self.k.active_streams['0'].health_monitor.stats['state'],'BUFFERING')
        with patch('regular_browser.time.time',return_value=1303):await self.k._recover_no_video(self.p)
        self.k.command.assert_not_awaited()
        await self.health(1304,11,frames=40,stall=301)
        self.assertEqual(self.k.active_streams['0'].health_monitor.stats['state'],'LOADING')
        with patch('regular_browser.time.time',return_value=1304):await self.k._recover_no_video(self.p)
        self.k.command.assert_not_awaited();self.k._recovery_sources.assert_not_awaited()
        # Eligibility independently rejects even a raw BUFFERING report when its
        # last observed progress is over five minutes old.
        self.k._frames[('0',0)][1]['state']='BUFFERING'
        self.assertFalse(self.k._recoverable_failure('0',1304))
