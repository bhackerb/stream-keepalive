import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from playback_reliability import PlaybackReliability
from catalog_providers import Catalog
import test_regular_browser as fixtures


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 2_000_000
        self.sample_clock = self.now
        self.path = Path(self.tmp.name)/'reliability.json'
        self.r = PlaybackReliability(self.path, clock=lambda: max(self.now, self.sample_clock))

    def sample(self, at, state='PLAYING', network_ok=True, **extra):
        self.sample_clock = at
        h = dict(state=state, current_time=at if state == 'PLAYING' else 10, decoded_frames=at if state == 'PLAYING' else 10)
        h.update(extra)
        self.r.observe('game', 'https://feed.example/game', 'test', 'basketball', 1, h, at, network_ok=network_ok)

    def test_sparse_neutral_then_good_progress_upgrades_and_persists(self):
        self.sample(self.now)
        for i in range(1, 20):self.sample(self.now+i*10)
        self.assertEqual(self.r.rating('test','basketball','https://feed.example/game')['score'], .5)
        for i in range(20, 181):self.sample(self.now+i*10)
        self.now += 1800
        self.r.save()
        fresh = PlaybackReliability(self.path, clock=lambda:self.now)
        self.assertGreater(fresh.rating('test','basketball','https://feed.example/game')['score'], .9)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(fresh.rating('test','hockey','https://feed.example/game')['score'], .5)
        self.now += 15*86400
        self.assertEqual(fresh.rating('test','basketball','https://feed.example/game')['score'], .5)

    def test_confirmed_buffering_downgrades_but_initial_blocking_does_not(self):
        for i in range(100):self.sample(self.now+i*10,'BUFFERING')
        self.assertFalse(self.r.rows)
        self.sample(self.now+1000)
        self.sample(self.now+1010)
        for i in range(102, 300):self.sample(self.now+i*10,'BUFFERING')
        self.now += 3000
        self.assertLess(self.r.rating('test','basketball','https://feed.example/game')['score'], .2)

    def test_no_penalties_for_network_pause_stale_or_unchanged_playing(self):
        self.sample(self.now)
        self.sample(self.now+10)
        baseline=sum(r['playing']+r['buffering'] for r in self.r.rows.values())
        for i in range(2,50):self.sample(self.now+i*10,'BUFFERING',network_ok=False)
        self.sample(self.now+500, paused=True)
        self.sample(self.now+510,'BUFFERING')
        self.sample(self.now+520,'BUFFERING')
        self.sample(self.now+600)
        self.sample(self.now+700)
        self.sample(self.now+710,current_time=self.now+700,decoded_frames=self.now+700)
        self.assertEqual(sum(r['playing']+r['buffering'] for r in self.r.rows.values()),baseline)

    def test_ranking_uses_feed_evidence_before_viewers(self):
        catalog=Catalog(Path(self.tmp.name)/'health.json')
        catalog.reliability=self.r
        sources=[dict(provider='test',url='https://feed.example/game',viewers=1,language='English',hd=True),
                 dict(provider='other',url='https://other.example/',viewers=10000,language='English',hd=True)]
        for i in range(181):self.sample(self.now+i*10)
        self.now+=1800
        self.assertEqual(catalog.rank(sources,'basketball')[0]['provider'],'test')
        self.assertEqual(catalog.rank(sources,'hockey')[0]['provider'],'other')

    def test_startup_and_run_metrics_reset_on_pause(self):
        self.r.opened('game','https://feed.example/game')
        for i in range(4):self.sample(self.now+i*10)
        self.sample(self.now+40,paused=True)
        for i in range(5,8):self.sample(self.now+i*10)
        data=self.r.evidence('test','basketball','https://feed.example/game')
        self.assertEqual(data['startup_successes'],1)
        self.assertEqual(data['mean_startup_seconds'],10)
        self.assertEqual(data['longest_run_seconds'],30)

    def test_disable_and_corrupt_optional_metrics(self):
        with patch.dict('os.environ',{'STREAMKEEPER_RELIABILITY_DISABLED':'1'}):
            self.assertEqual(self.r.rating('test','basketball','x')['scope'],'disabled')
        self.path.write_text(json.dumps({'version':1,'rows':{'bad':dict(at=self.now,playing=20,buffering=0,
            provider='test',sport='basketball',feed='x',startup_seconds='corrupt')}}))
        self.assertFalse(PlaybackReliability(self.path,clock=lambda:self.now).rows)

    def test_file_only_changes_when_dirty_and_at_most_once_per_minute(self):
        self.sample(self.now)
        self.sample(self.now+10)
        os.utime(self.path,ns=(1_000_000_000,1_000_000_000))
        first=self.path.stat().st_mtime_ns
        for i in range(2,7):
            self.sample(self.now+i*10)
            self.r.save()
            self.assertEqual(self.path.stat().st_mtime_ns,first)
        self.sample(self.now+70)
        second=self.path.stat().st_mtime_ns
        self.assertNotEqual(first,second)
        # No observation changes: even after a minute/day, an idle tick is read-only.
        for i in (80,140,86400):
            self.sample_clock=self.now+i
            self.r.save()
            self.assertEqual(self.path.stat().st_mtime_ns,second)

    def test_index_reused_for_all_sources_and_invalidated_by_observation(self):
        for i in range(181):self.sample(self.now+i*10)
        self.r.rating('test','basketball','https://feed.example/game')
        index=self.r._index
        for i in range(1000):self.r.rating('other','basketball',f'https://other.example/{i}')
        self.assertIs(self.r._index,index)
        self.sample(self.now+1810)
        self.assertIsNone(self.r._index)
        self.r.rating('test','basketball','https://feed.example/game')
        self.assertIsNot(self.r._index,index)


class KeeperEvidenceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RegularTests.asyncSetUp
    asyncTearDown = fixtures.RegularTests.asyncTearDown
    plan = fixtures.RegularTests.plan
    async def test_primary_only_passive_tick_and_no_control(self):
        await self.k._reconcile(self.plan(1))
        stream=self.k.active_streams['0']
        self.k.catalog.last={'matches':[dict(category='hockey',sources=[fixtures.source(stream.url)])]}
        self.k._browser_ready['firefox']=True
        self.k.command.reset_mock()
        with patch('regular_browser.time.time',return_value=1000):
            self.k._frames[('0',1)]=(1000,dict(state='PAUSED',width=1920,height=1080,current_time=2))
            self.k._frames[('0',2)]=(1000,dict(state='PLAYING',width=320,height=180,current_time=50))
            await self.k._reliability_tick()
        self.assertFalse(self.k.catalog.reliability.rows)
        self.k.command.assert_not_awaited()

    async def test_real_tick_collects_primary_progress_and_rechecks_network(self):
        await self.k._reconcile(self.plan(1))
        stream=self.k.active_streams['0']
        self.k.catalog.last={'matches':[dict(category='hockey',sources=[fixtures.source(stream.url)])]}
        self.k._browser_ready['firefox']=True
        self.k.resources.snapshot={'state':'ok'}
        response=MagicMock(status=200)
        cm=MagicMock();cm.__aenter__=AsyncMock(return_value=response);cm.__aexit__=AsyncMock(return_value=False)
        self.k._client=MagicMock();self.k._client.get.return_value=cm
        self.k.command.reset_mock()
        for at,state in [(1000,'PLAYING'),(1010,'PLAYING'),(1020,'BUFFERING'),(1030,'BUFFERING')]:
            self.k.catalog.reliability.clock=lambda at=at:at
            h=dict(state=state,width=1920,height=1080,current_time=at if state=='PLAYING'else 1010)
            with patch('regular_browser.time.time',return_value=at):
                self.k._frames[('0',1)]=(at,h)
                await self.k._reliability_tick()
        rows=self.k.catalog.reliability.rows.values()
        self.assertEqual(sum(r['buffering'] for r in rows),10)
        self.assertEqual(self.k._client.get.call_count,2)
        self.k.command.assert_not_awaited()
