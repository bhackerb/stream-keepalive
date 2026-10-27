import unittest
from types import SimpleNamespace

import regular_browser
from regular_browser import RegularBrowserKeeper

NOW = 1_000_000.0


def keeper(state='NO_VIDEO', started_at=NOW - 600):
    k = RegularBrowserKeeper.__new__(RegularBrowserKeeper)
    k.session = {'id': 's'}
    k.suppressed = set()
    k._ownership_lost = set()
    k._releasing = set()
    k._visible_ids = ('g1',)
    k._browser_ready = {'firefox': True}
    k._frames = {}
    k._last_seen = {}
    k.active_streams = {'g1': SimpleNamespace(
        id='g1', session_id='s', parked=False, browser='firefox', started_at=started_at,
        health_monitor=SimpleNamespace(stats={'state': state}))}
    return k


def frame(frame_id, age, **health):
    return (('g1', frame_id), (NOW - age, dict(health)))


class SilenceAndDeadSourceTests(unittest.TestCase):
    def check(self, k, now=NOW):
        return k._recoverable_failure('g1', now)

    def test_silence_after_no_video_is_a_failure(self):
        k = keeper('NO_VIDEO')
        k._last_seen['g1'] = NOW - 31
        self.assertTrue(self.check(k))

    def test_short_silence_is_not_yet_a_failure(self):
        k = keeper('NO_VIDEO')
        k._last_seen['g1'] = NOW - 10
        self.assertFalse(self.check(k))

    def test_silence_after_playing_is_never_a_failure(self):
        k = keeper('PLAYING')
        k._last_seen['g1'] = NOW - 600
        self.assertFalse(self.check(k))

    def test_recent_playing_frame_blocks_recovery_until_it_ages_out(self):
        k = keeper('NO_VIDEO')
        k._frames = dict([frame('top', age=60, state='PLAYING', width=1280, height=720)])
        k._last_seen['g1'] = NOW - 60
        self.assertFalse(self.check(k))
        k._frames = dict([frame('top', age=200, state='PLAYING', width=1280, height=720)])
        k._last_seen['g1'] = NOW - 200
        self.assertTrue(self.check(k))

    def test_silence_uses_start_time_when_nothing_was_ever_reported(self):
        k = keeper('NO_VIDEO', started_at=NOW - 45)
        self.assertTrue(self.check(k))

    def test_stale_frame_does_not_block_a_fresh_failing_frame(self):
        k = keeper('NO_VIDEO')
        k._frames = dict([frame('dead', age=200, state='PLAYING', width=1280, height=720),
                          frame('live', age=1, state='NO_VIDEO')])
        self.assertTrue(self.check(k))

    def test_dead_source_page_is_a_failure_at_once(self):
        k = keeper('NO_VIDEO')
        k._frames = dict([frame('embed', age=1, state='NO_VIDEO', page_error=True)])
        self.assertTrue(self.check(k))
        self.assertTrue(k._dead_source('g1', NOW))

    def test_healthy_playing_frame_is_not_a_failure(self):
        k = keeper('PLAYING')
        k._frames = dict([frame('top', age=1, state='PLAYING', width=1280, height=720)])
        self.assertFalse(self.check(k))
        self.assertFalse(k._dead_source('g1', NOW))


if __name__ == '__main__':
    unittest.main()
