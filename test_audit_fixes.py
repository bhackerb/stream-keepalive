import unittest
from unittest.mock import AsyncMock,patch
from aiohttp.test_utils import TestClient,TestServer
from api_server import StreamKeeperAPI
from regular_browser import stream_rank
import test_regular_browser as fixtures

class AuditFixTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.RegularTests.asyncSetUp
    asyncTearDown=fixtures.RegularTests.asyncTearDown
    plan=fixtures.RegularTests.plan

    def test_decorated_english_labels_win_ties_only(self):
        for label in ('English','English - ESPN+',' english (us)','ENGLISH-Sky'):self.assertTrue(stream_rank({'language':label})[1],label)
        for label in ('Englishman','Main','Spanish',None):self.assertFalse(stream_rank({'language':label})[1],label)
        self.assertGreater(stream_rank({'language':'Main','viewers':10}),stream_rank({'language':'English - ESPN+','viewers':9}))

    async def test_missing_playback_url_uses_ranked_lookup(self):
        self.k._client=object()
        self.k._recovery_sources=AsyncMock(return_value='https://streamed.pk/watch/0/admin/4')
        await self.k._reconcile(self.plan(1))
        self.k._recovery_sources.assert_awaited_once()
        self.assertEqual(self.k.active_streams['0'].url,'https://streamed.pk/watch/0/admin/4')

    async def test_failed_or_empty_lookup_falls_back_to_first_source(self):
        self.k._client=object()
        for effect in (RuntimeError('boom'),None):
            self.k.active_streams.clear()
            self.k._recovery_sources=AsyncMock(side_effect=effect)if isinstance(effect,Exception)else AsyncMock(return_value=None)
            await self.k._reconcile(self.plan(1))
            self.assertEqual(self.k.active_streams['0'].url,'https://streamed.pk/watch/0/admin/1')

    async def test_close_during_lookup_blocks_open(self):
        self.k._client=object()
        async def closed(*_):self.k.suppressed.add('0');return 'https://streamed.pk/watch/0/admin/4'
        self.k._recovery_sources=AsyncMock(side_effect=closed)
        await self.k._reconcile(self.plan(1))
        self.assertNotIn('0',self.k.active_streams)
        self.assertNotIn('open',[c.args[1]for c in self.k.command.await_args_list])

    async def test_bulk_reload_skips_budgeted_streams(self):
        await self.k._reconcile(self.plan(2));self.k.command.reset_mock()
        self.k._reload_times['0']=__import__('time').time()
        message=await self.k.force_reload()
        self.assertEqual([c.kwargs['stream_id']for c in self.k.command.await_args_list],['1'])
        self.assertIn('skipped',message)
        self.assertTrue((await self.k.force_reload('0')).startswith('❌'))

    async def test_all_control_routes_fail_closed_without_key(self):
        config={**self.config,'api':{'enabled':True,'api_key':''}}
        async with TestClient(TestServer(StreamKeeperAPI(self.k,config).app))as c:
            for path in ('/stop','/watch','/reload','/unmute'):
                self.assertEqual((await c.post(path,json={})).status,503,path)
            self.assertEqual((await c.get('/status')).status,503)
