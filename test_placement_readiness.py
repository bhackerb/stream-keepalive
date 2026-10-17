"""Hermetic regular-tab startup placement: no browser, site, or native WM calls."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from regular_browser import RegularBrowserKeeper
import test_regular_browser as fixtures


class PlacementReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.display_patch=patch('regular_browser.probe_displays',side_effect=lambda displays,*_: {'state':'available','displays':displays,'availability':{n:'connected'for n in displays},'outputs':[]})
        self.display_patch.start();self.addCleanup(self.display_patch.stop)
        self.tmp=tempfile.TemporaryDirectory()
        self.k=RegularBrowserKeeper({'layout':{'backend':'kwin','displays':{
            'widescreen':dict(x=1152,y=383,width=3440,height=1440)}},
            'session':{'state_file':str(Path(self.tmp.name)/'state.json')}})
        self.k.session={'id':'session','spec':{'browser':'firefox','display':'dock','sports':['hockey','baseball']},'until_ms':5000000}
        self.receipt={'tabId':10,'windowId':20,'token':'unpredictable-token'}
        self.ready=False;self.fail='Could not establish connection. Receiving end does not exist.'
        self.audit=dict(self.receipt);self.ops=[];self.markers=[]
        async def command(browser,op,**args):
            self.ops.append((op,args.get('stream_id')))
            if op=='open':return dict(self.receipt)
            if op=='inventory':return {'0':dict(self.audit)}if self.audit is not None else {}
            if op=='title':return 'Game'
            if op=='marker':
                self.markers.append(args['value'])
                if not self.ready:raise ValueError(self.fail)
                return True
            raise AssertionError('Unexpected operation: '+op)
        self.k.command=AsyncMock(side_effect=command)
        self.run=AsyncMock(side_effect=lambda plans,**_: {'success':True,'windows':[{'geometry':{k:p[k]for k in ('x','y','width','height')},'borderless':True}for p in plans]})
        self.patches=[patch('kwin_layout.run_script',self.run),patch('regular_browser.PLACEMENT_RETRY_INTERVAL',.001)]
        for p in self.patches:p.start()
        self.k.discover=AsyncMock(return_value=fixtures.RegularTests.plan(self,1))
        self.body={'mode':'manual','start':True,'presets':{'widescreen':1},'assignments':[{'game_id':'0','display':'widescreen','slot':0}]}

    async def asyncTearDown(self):
        task=self.k._placement_retry_task
        if task:
            task.cancel();await asyncio.gather(task,return_exceptions=True)
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()

    async def test_new_loading_tab_is_pending_then_placed_once_without_reopening_or_reloading(self):
        result=await self.k.set_session_layout(self.body)
        self.assertTrue(result['success']);self.assertTrue(result['placement_pending'])
        self.assertEqual(result['errors'],[]);self.assertTrue(self.k.layout_status['pending'])
        self.run.assert_not_awaited()
        self.ready=True
        await asyncio.wait_for(self.k._placement_retry_task,2)
        self.assertTrue(self.k.layout_status['success']);self.assertFalse(self.k.layout_status.get('pending'))
        self.assertEqual(self.run.await_count,3) # identify, move, verify
        self.assertEqual(sum(op=='open'for op,_ in self.ops),1)
        self.assertTrue(all(op in ('open','title','marker','inventory')for op,_ in self.ops))
        self.assertEqual(self.k.layout_status['placements'][0]['width'],3440)
        self.assertEqual(self.k.layout_status['placements'][0]['height'],1440)
        await self.k._reconcile(self.k.plan)
        self.assertEqual(self.run.await_count,3) # successful retry committed the signature
        self.assertEqual(self.k.errors,[])

    async def test_startup_about_blank_ownership_refusal_is_pending_only_with_same_receipt(self):
        self.fail='Owned stream is unavailable'
        result=await self.k.set_session_layout(self.body)
        self.assertTrue(result['placement_pending']);self.assertEqual(result['errors'],[])
        self.assertIn(('inventory',None),self.ops)
        self.ready=True;await asyncio.wait_for(self.k._placement_retry_task,2)
        self.assertTrue(self.k.layout_status['success'])

    async def test_permanent_receiver_failure_expires_without_native_movement_or_duplicate_tabs(self):
        with patch('regular_browser.PLACEMENT_READY_TIMEOUT',.02):
            result=await self.k.set_session_layout(self.body)
            self.assertTrue(result['placement_pending'])
            await asyncio.wait_for(self.k._placement_retry_task,1)
        self.assertFalse(self.k.layout_status['success']);self.assertFalse(self.k.layout_status.get('pending'))
        self.assertTrue(self.k.errors);self.run.assert_not_awaited()
        self.assertEqual(sum(op=='open'for op,_ in self.ops),1)
        self.assertLess(self.k.command.await_count,100)
        self.assertTrue(all(op in ('open','title','marker','inventory')for op,_ in self.ops))

    async def test_missing_or_changed_exact_tab_never_retries_takes_over_or_reopens(self):
        for audit in (None,{**self.receipt,'tabId':99},{**self.receipt,'token':'another-owner'}):
            with self.subTest(audit=audit):
                self.audit=audit
                await self.k._open('0','https://streamed.pk/watch/0','Game','firefox','session')
                placements=[dict(stream_id='0',display='widescreen',x=0,y=0,width=3440,height=1440)]
                result=await self.k._place(placements)
                self.assertFalse(result['success']);self.assertFalse(result.get('pending'))
                self.assertIsNone(self.k._placement_retry_task)
        self.run.assert_not_awaited();self.assertEqual(sum(op=='open'for op,_ in self.ops),1)

    async def test_old_stream_missing_receiver_stays_a_real_failure(self):
        self.k._add('0',dict(url='https://streamed.pk/watch/0',browser='firefox'))
        result=await self.k._place([dict(stream_id='0',x=0,y=0,width=3440,height=1440)])
        self.assertFalse(result['success']);self.assertFalse(result.get('pending'))
        self.assertNotIn(('inventory',None),self.ops);self.assertIsNone(self.k._placement_retry_task)
        self.run.assert_not_awaited()

    async def test_pending_retry_stops_after_session_change_without_touching_other_games(self):
        self.k._add('healthy',dict(url='https://streamed.pk/watch/healthy',browser='firefox'))
        result=await self.k.set_session_layout(self.body)
        self.assertTrue(result['placement_pending'])
        count=len(self.ops);self.k.session=None
        await asyncio.wait_for(self.k._placement_retry_task,1)
        self.assertEqual(len(self.ops),count);self.assertIn('healthy',self.k.active_streams)
        self.run.assert_not_awaited();self.assertFalse(self.k.layout_status.get('pending'))

    async def test_pending_retry_stops_after_owned_tab_is_closed(self):
        await self.k.set_session_layout(self.body)
        await self.k._event('firefox',{'event':'closed','stream_id':'0'})
        count=len(self.ops);await asyncio.wait_for(self.k._placement_retry_task,1)
        self.assertEqual(len(self.ops),count);self.assertIn('0',self.k.suppressed)
        self.run.assert_not_awaited()

    async def test_new_layout_supersedes_pending_coordinates(self):
        await self.k.set_session_layout(self.body)
        previous=self.k._placement_retry_task
        self.ready=True
        await self.k.set_session_layout({**self.body,'presets':{'widescreen':4}})
        self.assertTrue(previous.cancelled())
        self.assertTrue(self.k.layout_status['success'])
        self.assertEqual(self.k.layout_status['placements'][0]['width'],1720)
        self.assertEqual(self.run.await_count,3)
        self.assertEqual(sum(op=='open'for op,_ in self.ops),1)

    async def test_unexpected_background_failure_leaves_honest_failure_not_perpetual_pending(self):
        await self.k.set_session_layout(self.body)
        self.ready=True;self.run.side_effect=RuntimeError('Compositor unavailable')
        await asyncio.wait_for(self.k._placement_retry_task,1)
        self.assertFalse(self.k.layout_status['success']);self.assertFalse(self.k.layout_status.get('pending'))
        self.assertIn('Compositor unavailable',self.k.errors[0])
        self.assertEqual(sum(op=='open'for op,_ in self.ops),1)

    async def test_failed_inventory_blocks_retry(self):
        self.k._inventory_errors['firefox']=['0: ownership lookup failed']
        result=await self.k.set_session_layout(self.body)
        self.assertFalse(result['placement_pending']);self.assertTrue(result['errors'])
        self.assertIsNone(self.k._placement_retry_task);self.run.assert_not_awaited()


if __name__=='__main__':unittest.main()
