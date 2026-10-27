import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import regular_browser
from regular_browser import RegularBrowserKeeper


def keeper(tmp, config=None):
    # Bypass __init__ (it opens browser connections); the ladder only needs these attributes.
    k = RegularBrowserKeeper.__new__(RegularBrowserKeeper)
    k.config = config or {}
    k.state_path = Path(tmp) / 'regular-session.json'
    k.active_streams = {}
    k._releasing = set()
    k._frames = {}
    k._autoplay_tasks = set()
    k.display_probe = {'displays': {'tv': {'x': 0, 'y': 0, 'width': 3840, 'height': 2160}}}
    return k


def stream(sid='s1', site='onhockey.tv'):
    stats = {'state': 'NO_VIDEO'}
    return SimpleNamespace(id=sid, site=site, health_monitor=SimpleNamespace(stats=stats))


class AutoplayLadderTests(unittest.TestCase):
    def test_steps_default_and_config_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(keeper(tmp)._autoplay_steps(), regular_browser.AUTOPLAY_STEPS)
            filtered = keeper(tmp, {'session': {'autoplay_steps': ['os_click', 'play']}})._autoplay_steps()
            # Filtered in the canonical ladder order, not the config order.
            self.assertEqual(filtered, ('play', 'os_click'))
            self.assertEqual(keeper(tmp, {'session': {'autoplay_steps': ['bogus']}})._autoplay_steps(), regular_browser.AUTOPLAY_STEPS)

    def test_ladder_stops_at_first_step_that_advances(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = keeper(tmp)
            s = stream()
            k.active_streams['s1'] = s
            ran = []

            async def step(sid, name):
                ran.append(name)

            outcomes = iter(['stalled', 'advanced'])

            async def wait(sid, started, timeout):
                return next(outcomes)

            k._autoplay_step = step
            k._wait_for_advance = wait
            with patch.object(regular_browser, 'AUTOPLAY_GRACE', 0):
                asyncio.run(k._run_autoplay_ladder('s1'))
            self.assertEqual(ran, ['play', 'play_control'])
            self.assertEqual(s.health_monitor.stats['autoplay'], {'step': 'play_control', 'state': 'playing'})

    def test_ladder_reports_needs_click_after_all_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = keeper(tmp)
            s = stream()
            k.active_streams['s1'] = s
            k._autoplay_step = AsyncMock()

            async def wait(sid, started, timeout):
                return 'no_telemetry'

            k._wait_for_advance = wait
            with patch.object(regular_browser, 'AUTOPLAY_GRACE', 0):
                asyncio.run(k._run_autoplay_ladder('s1'))
            self.assertEqual(k._autoplay_step.await_count, len(regular_browser.AUTOPLAY_STEPS))
            self.assertEqual(s.health_monitor.stats['autoplay'], {'step': None, 'state': 'needs_click'})
            lines = (Path(tmp) / 'autoplay-outcomes.jsonl').read_text().splitlines()
            self.assertEqual(len(lines), len(regular_browser.AUTOPLAY_STEPS))
            self.assertIn('"outcome": "no_telemetry"', lines[-1])

    def test_wait_for_advance_needs_fresh_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = keeper(tmp)
            now = 100.0

            async def run_with_heartbeats():
                # Two fresh heartbeats arrive while the step is waiting.
                k._frames[('s1', 1)] = (now + 1, {'width': 1280, 'height': 720, 'state': 'PLAYING', 'current_time': 5, 'decoded_frames': 10})
                waiter = asyncio.create_task(k._wait_for_advance('s1', now, 2))
                await asyncio.sleep(0.05)
                k._frames[('s1', 1)] = (now + 2, {'width': 1280, 'height': 720, 'state': 'PLAYING', 'current_time': 9, 'decoded_frames': 40})
                return await waiter

            self.assertEqual(asyncio.run(run_with_heartbeats()), 'advanced')
            # Frames observed before the step started never count as progress.
            k._frames = {('s1', 1): (now - 5, {'width': 1280, 'height': 720, 'state': 'PLAYING', 'current_time': 9, 'decoded_frames': 40})}
            self.assertEqual(asyncio.run(k._wait_for_advance('s1', now, 0.2)), 'no_telemetry')

    def test_frame_click_uses_iframe_center_in_screen_pixels(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = keeper(tmp)
            geometry = {'dpr': 2, 'offset_x': 100, 'offset_y': 50,
                        'rects': [{'tag': 'IFRAME', 'x': 10, 'y': 20, 'w': 710, 'h': 400}]}
            k.command_for = AsyncMock(return_value=geometry)
            clicked = []
            with patch.object(regular_browser, 'click_point', side_effect=lambda x, y: clicked.append((x, y))):
                asyncio.run(k._frame_click('s1'))
            # ((100 + 10 + 355) * 2, (50 + 20 + 200) * 2)
            self.assertEqual(clicked, [(930, 540)])

    def test_frame_click_refuses_points_off_displays(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = keeper(tmp)
            k.command_for = AsyncMock(return_value={'dpr': 1, 'offset_x': 5000, 'offset_y': 0,
                                                    'rects': [{'tag': 'IFRAME', 'x': 0, 'y': 0, 'w': 710, 'h': 400}]})
            with patch.object(regular_browser, 'click_point') as click:
                with self.assertRaises(ValueError):
                    asyncio.run(k._frame_click('s1'))
                click.assert_not_called()

    def test_embedded_unmute_never_clicks_mute_controls(self):
        with tempfile.TemporaryDirectory() as tmp:
            k = keeper(tmp)
            k.command_for = AsyncMock(return_value={'success': True})
            self.assertEqual(asyncio.run(k._embedded_unmute('s1')), ['volume_control'])
            k.command_for.assert_awaited_with('s1', 'volume_control')


if __name__ == '__main__':
    unittest.main()
