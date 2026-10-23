import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from native_player import player_command
from regular_browser import RegularBrowserKeeper
from regular_browser import validate_url
from resource_monitor import GIB, ResourceMonitor


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.proc = Path(self.tmp.name)
        self.mem(64, 50)
        (self.proc / 'pressure').mkdir()
        (self.proc / 'pressure/memory').write_text('some avg10=0.00 avg60=0.00 avg300=0.00 total=0\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n')

    def mem(self, total, available):
        (self.proc / 'meminfo').write_text(f'MemTotal: {int(total*GIB/1024)} kB\nMemAvailable: {int(available*GIB/1024)} kB\nSwapTotal: 65536 kB\nSwapFree: 32768 kB\n')

    def browser(self, pid, rss, swap=0, ticks=0, start='100'):
        p = self.proc / str(pid)
        p.mkdir(exist_ok=True)
        if not (p / 'exe').is_symlink():
            (p / 'exe').symlink_to('/usr/lib/firefox/firefox')
        (p / 'status').write_text(f'Name:\tWeb Content\nState:\tS (sleeping)\nx86_Thread_features:\nVmRSS: {int(rss*GIB/1024)} kB\nVmSwap: {int(swap*GIB/1024)} kB\n')
        stat = ['0'] * 22
        stat[11], stat[12], stat[19] = str(ticks), '0', start
        (p / 'stat').write_text(f'{pid} (Web Content) ' + ' '.join(stat))

    def test_low_available_ram_and_compressed_swap_are_reported(self):
        self.mem(64, 5)
        result = ResourceMonitor(self.proc).sample()
        self.assertEqual(result['state'], 'pressure')
        self.assertEqual(result['memory']['swap_used_bytes'], 32768*1024)

    def test_browser_runaway_and_top_processes_are_bounded(self):
        for pid in range(10, 18):
            self.browser(pid, 3, 2)
        result = ResourceMonitor(self.proc).sample()
        self.assertEqual(result['state'], 'pressure')
        self.assertEqual(len(result['browsers']['firefox']['largest_processes']), 5)
        self.assertEqual(result['browsers']['firefox']['processes'], 8)
        self.assertNotIn('url', json.dumps(result))

    def test_psi_blocks_even_with_free_ram(self):
        (self.proc / 'pressure/memory').write_text('some avg10=30.00 avg60=0 avg300=0 total=0\n')
        self.assertEqual(ResourceMonitor(self.proc).sample()['state'], 'pressure')

    def test_cpu_delta_and_recycled_pid(self):
        ticks = os.sysconf('SC_CLK_TCK')
        self.browser(10, 1)
        monitor = ResourceMonitor(self.proc, clock=iter([10, 12, 14]).__next__)
        monitor.sample()
        self.browser(10, 1, ticks=ticks*2)
        self.assertEqual(monitor.sample()['browsers']['firefox']['cpu_percent'], 100)
        self.browser(10, 1, ticks=5000, start='200')
        self.assertEqual(monitor.sample()['browsers']['firefox']['cpu_percent'], 0)

    def test_missing_memory_is_explicit_and_exited_process_is_ignored(self):
        (self.proc / '11').mkdir()
        self.assertEqual(ResourceMonitor(self.proc).sample()['state'], 'ok')
        (self.proc / 'meminfo').unlink()
        self.assertEqual(ResourceMonitor(self.proc).sample()['state'], 'unavailable')

    def test_optional_psi_and_no_processes_still_allow_memory_check(self):
        (self.proc / 'pressure/memory').unlink()
        self.assertEqual(ResourceMonitor(self.proc).sample()['state'], 'ok')

    def test_updated_browser_executable_is_still_counted_until_restart(self):
        self.browser(10, 1)
        (self.proc / '10/exe').unlink()
        (self.proc / '10/exe').symlink_to('/usr/lib/firefox/firefox (deleted)')
        self.assertEqual(ResourceMonitor(self.proc).sample()['browsers']['firefox']['rss_bytes'], GIB)


class GuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.k = RegularBrowserKeeper({'streams': {'max_streams': 2}, 'session': {'state_file': str(Path(self.tmp.name)/'state.json')}})
        self.k.command = AsyncMock(return_value=True)
        self.k.refresh_resources = AsyncMock(return_value={'state': 'ok', 'message': 'ok'})

    async def test_capacity_applies_to_concurrent_manual_and_internal_opens(self):
        await asyncio.gather(*(self.k._open(str(i), 'https://streamed.pk/watch/game', 'Game', 'firefox') for i in range(2)))
        with self.assertRaisesRegex(ValueError, 'capacity'):
            await self.k._open('third', 'https://streamed.pk/watch/game', 'Game', 'firefox')
        self.assertEqual(self.k.command.await_count, 2)
        self.assertEqual(len(self.k.active_streams), 2)

    async def test_pressure_blocks_open_and_automatic_recovery_without_touching_existing(self):
        await self.k._open('game', 'https://streamed.pk/watch/game', 'Game', 'firefox')
        self.k.command.reset_mock()
        self.k.refresh_resources.return_value = {'state': 'pressure', 'message': 'Low RAM'}
        with self.assertRaisesRegex(ValueError, 'Low RAM'):
            await self.k._open('second', 'https://streamed.pk/watch/second', 'Game', 'firefox')
        self.k.session = {'id': 's'}
        await self.k._reconcile({})
        await self.k._recover_no_video({})
        self.k.command.assert_not_awaited()
        self.assertFalse(self.k.active_streams['game'].parked)
        self.assertEqual(list(self.k.active_streams), ['game'])

    async def test_unavailable_check_blocks_new_open_but_does_not_block_stop(self):
        await self.k._open('game', 'https://streamed.pk/watch/game', 'Game', 'firefox')
        self.k.refresh_resources.return_value = {'state': 'unavailable', 'message': 'No sample'}
        with self.assertRaisesRegex(ValueError, 'No sample'):
            await self.k._open('second', 'https://streamed.pk/watch/second', 'Game', 'firefox')
        await self.k.stop('game')
        self.assertFalse(self.k.active_streams)

    async def test_pressure_recovers_and_explicit_override_is_available(self):
        self.k.refresh_resources.return_value = {'state': 'pressure', 'message': 'Low RAM'}
        self.assertIn('deferred', await self.k.watch_url('https://streamed.pk/watch/game'))
        self.k.refresh_resources.return_value = {'state': 'ok', 'message': 'ok'}
        self.assertNotIn('❌', await self.k.watch_url('https://streamed.pk/watch/game'))
        self.k.config['session']['memory_guard'] = False
        self.k.refresh_resources.return_value = {'state': 'pressure', 'message': 'Low RAM'}
        self.assertNotIn('❌', await self.k.watch_url('https://streamed.pk/watch/other'))

    async def test_metrics_follow_primary_video_and_legacy_values_clear(self):
        await self.k._open('game', 'https://streamed.pk/watch/game', 'Game', 'firefox')
        async def event(frame, width, **metrics):
            await self.k._event('firefox', {'event': 'health', 'stream_id': 'game', 'frame_id': frame, 'health': {'state': 'PLAYING', 'width': width, 'height': 1080, 'current_time': 10, **metrics}})
        await event(1, 1920, dropped_frames=12, buffer_ahead_seconds=8, live_edge_distance_seconds=20)
        await event(2, 320, dropped_frames=500, buffer_ahead_seconds=1)
        health = self.k.active_streams['game'].health_monitor.stats
        self.assertEqual(health['dropped_frames'], 12)
        self.assertEqual(health['live_edge_distance_seconds'], 20)
        await event(1, 1920, dropped_frames=float('nan'), buffer_ahead_seconds=-1)
        self.assertIsNone(health['dropped_frames'])
        self.assertIsNone(health['buffer_ahead_seconds'])
        self.assertIsNone(health['live_edge_distance_seconds'])

    async def test_onhockey_requires_new_companion_and_preserves_other_windows(self):
        url = 'https://www.onhockey.tv/np_stream400.php?channel=//example.com/live'
        with self.assertRaisesRegex(ValueError, '0.2.6'):
            await self.k._open('hockey', url, 'Hockey', 'firefox')
        self.k.command.assert_not_awaited()
        self.k._companion_info['firefox'] = {'capabilities': ['onhockey-v1']}
        await self.k._open('hockey', url, 'Hockey', 'firefox')
        self.assertEqual(self.k.active_streams['hockey'].site, 'onhockey.tv')
        for unsafe in ('https://onhockey.tv.evil.example/live', 'http://onhockey.tv/live', 'https://user:password@onhockey.tv/live'):
            with self.assertRaises(ValueError):
                validate_url(unsafe)


class NativePlayerTests(unittest.TestCase):
    def test_direct_media_uses_hardware_decode_bounded_cache_and_no_scripts(self):
        url = 'https://example.com/live.m3u8?token=abc&quality=720'
        command = player_command(url)
        self.assertEqual(command[-2:], ['--', url])
        for flag in ('--no-config', '--load-scripts=no', '--ytdl=no', '--hwdec=auto', '--demuxer-max-bytes=64MiB', '--mute=yes'):
            self.assertIn(flag, command)
        self.assertIn('--mute=no', player_command(url, unmute=True))

    def test_rejects_unsafe_protocols_credentials_ports_and_control_characters(self):
        for url in ('file:///etc/passwd', '--script=bad', 'http://example.com/a', 'https://user:pass@example.com/a', 'https://example.com:123/a', 'https://example.com/\na'):
            with self.assertRaises(ValueError):
                player_command(url)
