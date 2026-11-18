import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch

from regular_browser import RegularBrowserKeeper
from api_server import StreamKeeperAPI
import test_regular_browser as fixtures


class LaunchPeriodTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.RegularTests.asyncSetUp
    asyncTearDown=fixtures.RegularTests.asyncTearDown

    def add_stream(self,url='https://streamed.pk/watch/game/admin/1'):
        self.k._add('game',dict(url=url,browser='firefox',session_id='s'))
        self.k._browser_ready['firefox']=True
        return self.k.active_streams['game']

    def sample(self,now,position=None,state='PLAYING',frame=1,decoded=None,age=0):
        health=dict(state=state,width=1920,height=1080,current_time=now if position is None else position,decoded_frames=now*30 if decoded is None else decoded,paused=state=='PAUSED')
        self.k._frames[('game',frame)]=(now-age,health)
        with patch('regular_browser.time.time',return_value=now),patch('regular_browser.time.monotonic',return_value=now):
            self.k._launch_tick()
        return self.k.active_streams['game'].health_monitor.stats['launch']

    async def test_five_checks_fifteen_seconds_apart_promote_after_one_minute(self):
        self.add_stream()
        for now in (100,115,130,145):
            status=self.sample(now)
            self.assertEqual(status['phase'],'launch')
        self.assertEqual(self.sample(159)['checks'],4)
        status=self.sample(160)
        self.assertEqual(status['phase'],'normal')
        self.assertEqual(status['healthy_seconds'],60)
        self.assertEqual(status['checks'],5)
        self.sample(175,state='BUFFERING')
        self.assertEqual(status['checks'],5)  # normal care has taken over
        self.k.command.assert_not_awaited()

    async def test_no_video_paused_buffering_stale_frozen_and_disconnected_cannot_promote(self):
        for failure in ('NO_VIDEO','PAUSED','BUFFERING','stale','frozen','no_decoding','disconnected'):
            with self.subTest(failure=failure):
                stream=self.add_stream()
                self.sample(100);self.sample(115);self.sample(130)
                self.k._browser_ready['firefox']=failure!='disconnected'
                kwargs={'state':failure}if failure in ('NO_VIDEO','PAUSED','BUFFERING')else{}
                if failure=='stale':kwargs['age']=7
                if failure=='frozen':kwargs['position']=130
                if failure=='no_decoding':kwargs['decoded']=3900
                status=self.sample(145,**kwargs)
                self.assertEqual(status['phase'],'launch')
                self.assertEqual(status['healthy_seconds'],0)
                self.assertIs(stream,self.k.active_streams['game'])
        self.k.command.assert_not_awaited()

    async def test_short_buffer_between_checks_resets_launch_window(self):
        stream=self.add_stream()
        self.sample(100);self.sample(115);self.sample(130)
        with patch('regular_browser.time.time',return_value=135):
            await self.k._event('firefox',dict(event='health',stream_id='game',frame_id=1,health=dict(state='BUFFERING',width=1920,height=1080,current_time=130,decoded_frames=3900)))
        for now in (145,160,175,190):self.assertEqual(self.sample(now)['phase'],'launch')
        self.assertEqual(self.sample(205)['phase'],'normal')
        self.assertEqual(stream.health_monitor.stats['launch']['healthy_seconds'],60)

    async def test_frame_replacement_and_missed_checks_restart_the_window(self):
        self.add_stream();self.sample(100);self.sample(115)
        self.k._frames.clear()
        self.assertEqual(self.sample(130,frame=2)['healthy_seconds'],0)
        self.assertEqual(self.sample(161,frame=2)['healthy_seconds'],0)

    async def test_telemetry_gap_between_checks_restarts_launch_window(self):
        stream=self.add_stream();self.sample(100);self.sample(115);self.sample(130)
        with patch('regular_browser.time.time',return_value=140):
            await self.k._event('firefox',dict(event='health',stream_id='game',frame_id=1,health=dict(state='PLAYING',width=1920,height=1080,current_time=140,decoded_frames=4200)))
        self.assertEqual(stream.health_monitor.stats['launch']['healthy_seconds'],0)
        self.assertEqual(self.sample(145)['healthy_seconds'],0)

    async def test_source_replacement_reload_and_daemon_restart_require_new_launch(self):
        stream=self.add_stream()
        for now in (100,115,130,145,160):self.sample(now)
        self.assertEqual(stream.health_monitor.stats['launch']['phase'],'normal')
        self.k._save()
        fresh=RegularBrowserKeeper(self.config)
        self.assertEqual(fresh.active_streams['game'].health_monitor.stats['launch']['phase'],'launch')
        await self.k.force_reload('game')
        self.assertEqual(stream.health_monitor.stats['launch']['phase'],'launch')
        self.k._add('game',dict(url='https://streamed.pk/watch/game/admin/2',browser='firefox',session_id='s'))
        self.assertEqual(self.k.active_streams['game'].health_monitor.stats['launch']['checks'],0)

    async def test_parked_releasing_and_lost_streams_are_never_promoted(self):
        for flag in ('parked','_releasing','_ownership_lost','suppressed'):
            stream=self.add_stream()
            if flag=='parked':stream.parked=True
            else:getattr(self.k,flag).add('game')
            for now in (100,115,130,145,160):self.sample(now)
            self.assertEqual(stream.health_monitor.stats['launch']['phase'],'launch')
            if flag!='parked':getattr(self.k,flag).clear()
        self.k.command.assert_not_awaited()

    async def test_launch_task_runs_without_catalog_and_is_cancelled_on_shutdown(self):
        checked=asyncio.Event();blocked=asyncio.Event()
        self.k.refresh_resources=AsyncMock()
        self.k.refresh_displays=AsyncMock()
        self.k._loop=blocked.wait
        self.k._reliability_loop=blocked.wait
        self.k._launch_tick=checked.set
        with patch('regular_browser.LAUNCH_CHECK_INTERVAL',.001):
            try:
                await self.k.start_browser()
                await asyncio.wait_for(checked.wait(),1)
            finally:await self.k.shutdown()
        self.assertTrue(self.k._launch_task.cancelled())

    async def test_launch_loop_with_simulated_early_and_late_wakes(self):
        stream=self.add_stream();clock=[100.0];checks=[]
        jitter=iter((.5,-.2,0,.2,-.1,0,.1))
        tick=self.k._launch_tick
        def checked_tick():
            checks.append(clock[0]);tick()
        self.k._launch_tick=checked_tick
        async def sleep(delay):
            if len(checks)>=5:raise asyncio.CancelledError()
            try:extra=next(jitter)
            except StopIteration:raise asyncio.CancelledError()
            clock[0]+=delay+extra
            self.k._frames[('game',1)]=(clock[0],dict(state='PLAYING',width=1920,height=1080,current_time=clock[0],decoded_frames=clock[0]*30))
        with patch('regular_browser.asyncio.get_running_loop',return_value=SimpleNamespace(time=lambda:clock[0])),patch('regular_browser.asyncio.sleep',side_effect=sleep),patch('regular_browser.time.monotonic',side_effect=lambda:clock[0]),patch('regular_browser.time.time',side_effect=lambda:clock[0]):
            with self.assertRaises(asyncio.CancelledError):await self.k._launch_loop()
        self.assertEqual(stream.health_monitor.stats['launch']['phase'],'normal')
        self.assertEqual(stream.health_monitor.stats['launch']['checks'],5)
        self.assertTrue(all(15<=b-a<=16 for a,b in zip(checks,checks[1:])))

    async def test_normal_recovery_receives_only_promoted_streams(self):
        stream=self.add_stream();self.k._visible_ids=('game',)
        self.k._recovery['game']=dict(attempts=1,tried=[],since=1,last_action=1)
        self.k._check_memory=AsyncMock();self.k._periodic_refresh=AsyncMock()
        self.k.command=AsyncMock(return_value={'game':{}})
        self.k._recovery_sources=AsyncMock(return_value=None)
        plan={'live':[{'id':'game','sources':[]}]}
        with patch('regular_browser.time.time',return_value=1000):
            self.k._frames[('game',0)]=(1000,{'state':'NO_VIDEO'})
            await self.k._recover_no_video(plan,phase='normal')
            self.k.command.assert_not_awaited()
            await self.k._recover_no_video(plan,phase='launch')
            self.k._recovery_sources.assert_awaited_once()
            self.k.command.reset_mock();self.k._recovery_sources.reset_mock()
            stream.health_monitor.stats['launch']['phase']='normal'
            self.k._recovery['game']['last_action']=1
            await self.k._recover_no_video(plan,phase='launch')
            self.k.command.assert_not_awaited()
            await self.k._recover_no_video(plan,phase='normal')
            self.k._recovery_sources.assert_awaited_once()

    async def test_periodic_maintenance_keeps_normal_streams_after_handoff(self):
        stream=self.add_stream();stream.health_monitor.stats['launch']['phase']='normal'
        self.k._add('warming',dict(url='https://streamed.pk/watch/warming/admin/1',browser='firefox',session_id='s'))
        self.k._check_memory=AsyncMock();self.k._periodic_refresh=AsyncMock()
        self.k._recover_no_video=AsyncMock()
        row={'id':'game','sources':[]};warming={'id':'warming','sources':[]}
        with patch('regular_browser.time.time',return_value=1000):
            await self.k._recover_monitored_streams({'live':[row,warming]})
        self.k._periodic_refresh.assert_awaited_once_with({'game':row,'warming':warming})

    async def test_reconcile_preserves_existing_launch_progress_and_normal_phase(self):
        plan=fixtures.RegularTests.plan(self,1)
        await self.k._reconcile(plan)
        stream=self.k.active_streams['0'];status=stream.health_monitor.stats['launch']
        status['healthy_seconds']=30
        await self.k._reconcile(plan)
        self.assertEqual(status['healthy_seconds'],30)
        status['phase']='normal'
        await self.k._reconcile(plan)
        self.assertIs(self.k.active_streams['0'],stream)
        self.assertEqual(stream.health_monitor.stats['launch']['phase'],'normal')

    async def test_status_exposes_launch_phase_and_progress(self):
        stream=self.add_stream()
        api=StreamKeeperAPI(self.k,self.config)
        result=api._stream_to_dict(stream)
        self.assertEqual(result['health']['launch']['phase'],'launch')
        self.assertEqual(result['health']['launch']['check_interval_seconds'],15)
        for now in (100,115,130,145,160):self.sample(now)
        self.assertEqual(api._stream_to_dict(stream)['health']['launch']['phase'],'normal')
