"""Hermetic regression checks for embedded players and recovery debounce."""
import unittest
from unittest.mock import AsyncMock, patch
from health_monitor import HealthMonitor, HealthSnapshot, StreamState

class HealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_cross_origin_player_is_inspected(self):
        outer, player = AsyncMock(), AsyncMock()
        outer.evaluate.return_value = {'found': False}
        player.evaluate.return_value = {'found': True, 'readyState': 4, 'currentTime': 42,
                                        'videoWidth': 1920, 'videoHeight': 1080, 'volume': 1}
        page = type('Page', (), {'frames': [outer, player]})()
        monitor = HealthMonitor({})
        snap = await monitor.check_health(page)
        self.assertEqual(snap.state, StreamState.PLAYING)
        self.assertEqual(snap.video_width, 1920)
        self.assertTrue(await monitor.force_play(page))
        player.evaluate.assert_awaited()

    async def test_navigation_does_not_trigger_recovery(self):
        frame = AsyncMock()
        frame.evaluate.side_effect = Exception('Execution context destroyed by navigation')
        page = type('Page', (), {'frames': [frame]})()
        monitor = HealthMonitor({})
        self.assertEqual((await monitor.check_health(page)).state, StreamState.LOADING)
        self.assertFalse(monitor.needs_recovery())

    def test_brief_buffering_does_not_reload(self):
        monitor = HealthMonitor({'health': {'stall_threshold_seconds': 15}})
        for stamp in [100, 105, 110]:
            monitor.history.add(HealthSnapshot(timestamp=stamp, state=StreamState.STALLED))
        self.assertFalse(monitor.needs_recovery())
        monitor.history.add(HealthSnapshot(timestamp=115, state=StreamState.STALLED))
        self.assertTrue(monitor.needs_recovery())
        monitor.history.add(HealthSnapshot(timestamp=120, state=StreamState.PLAYING, current_time=5))
        self.assertFalse(monitor.needs_recovery())

class AutoAttachTests(unittest.IsolatedAsyncioTestCase):
    async def test_cross_origin_video_attaches_without_cdp(self):
        import asyncio
        from stream_keeper import StreamKeeper
        keeper = StreamKeeper({})
        outer, player = AsyncMock(), AsyncMock()
        outer.evaluate.return_value = False
        player.evaluate.return_value = True
        page = AsyncMock()
        page.url = 'https://streamed.pk/watch/test/admin/1'
        page.frames = [outer, player]
        page.title.return_value = 'Watch Test Stream Admin 1'
        keeper.context = type('Context', (), {'pages': [page]})()
        keeper._get_urls_with_video_cdp = AsyncMock(return_value=set())
        keeper._auto_arrange = AsyncMock()
        keeper._monitor_loop = AsyncMock()
        with patch('stream_keeper.asyncio.sleep', new=AsyncMock(side_effect=[None, None, asyncio.CancelledError()])):
            await keeper._auto_keepalive_loop(1)
        self.assertEqual(len(keeper.active_streams), 1)
        self.assertIs(next(iter(keeper.active_streams.values())).page, page)
        player.evaluate.assert_awaited_once()
        for stream in keeper.active_streams.values():
            await stream.monitor_task

class RecoveryStageTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_attempt_does_not_reload_or_switch(self):
        from stream_keeper import StreamKeeper, ActiveStream
        keeper = StreamKeeper({'health': {'screenshot_on_failure': False}})
        monitor = HealthMonitor({})
        monitor.force_play = AsyncMock(return_value=False)
        monitor.check_health = AsyncMock(return_value=HealthSnapshot(state=StreamState.LOADING))
        driver = AsyncMock()
        stream = ActiveStream('test', 'Test', AsyncMock(), driver, monitor)
        with patch('stream_keeper.asyncio.sleep', new=AsyncMock()):
            await keeper._attempt_recovery(stream, 'paused')
            driver.reload_stream.assert_not_awaited()
            driver.next_source.assert_not_awaited()
            driver.navigate_to_games.assert_not_awaited()
            monitor.history.last_recovery = 0
            await keeper._attempt_recovery(stream, 'stalled')
            driver.reload_stream.assert_awaited_once()
            driver.next_source.assert_not_awaited()
            driver.navigate_to_games.assert_not_awaited()

class FirefoxDOMTests(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_and_cross_origin_player(self):
        from playwright.async_api import async_playwright
        from ad_handler import AdHandler
        async with async_playwright() as p:
            browser = await p.firefox.launch(headless=True)
            try:
                page = await browser.new_page()
                async def fixture(route):
                    if route.request.url.startswith('https://player.example.test/'):
                        html = """<video muted></video><script>
                        const c=document.createElement('canvas'); c.width=320;c.height=180;
                        const v=document.querySelector('video');v.srcObject=c.captureStream(5);
                        setInterval(()=>{c.getContext('2d').fillRect(0,0,320,180)},100);
                        </script>"""
                    else:
                        html = """<div class="modal" style="position:fixed;z-index:10000;width:100vw;height:100vh">
                        <iframe src="https://player.example.test/embed/admin/1"></iframe></div>
                        <div class="ad-overlay" style="position:fixed;z-index:10001;width:100vw;height:100vh">ad</div>"""
                    await route.fulfill(content_type='text/html', body=html)
                await page.route('**/*', fixture)
                await page.goto('https://directory.example.test/watch/game')
                await AdHandler({}).dismiss_overlays(page)
                self.assertEqual(await page.locator('iframe').count(), 1)
                self.assertEqual(await page.locator('.modal').count(), 1)
                self.assertEqual(await page.locator('.ad-overlay').count(), 0)
                monitor = HealthMonitor({})
                self.assertTrue(await monitor.force_play(page))
                await page.frames[1].wait_for_function("document.querySelector('video').videoWidth > 0")
                snap = await monitor.check_health(page)
                self.assertEqual(snap.video_width, 320)
                self.assertEqual(snap.state, StreamState.PLAYING)
            finally:
                await browser.close()

if __name__ == '__main__':
    unittest.main()
