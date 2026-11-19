import unittest
from cast_mirror import CastMirror, decide, match_target


class DecideTests(unittest.TestCase):
    def test_policy(self):
        idle, busy = {'name': 'L', 'session': None}, {'name': 'L', 'session': 'YouTube'}
        self.assertEqual(decide(True, None, False), 'none')
        self.assertEqual(decide(True, idle, False), 'start')
        self.assertEqual(decide(True, busy, False), 'yield')
        self.assertEqual(decide(True, busy, True), 'none')
        self.assertEqual(decide(False, busy, True), 'stop')
        self.assertEqual(decide(False, busy, False), 'none')
        self.assertEqual(decide(False, idle, False), 'none')


class MatchTests(unittest.TestCase):
    def test_match(self):
        t = [{'type': 'page', 'id': 'a', 'url': 'https://x.com/p?1#sk-owned=t'}, {'type': 'page', 'id': 'b', 'url': 'https://y.com/'},
             {'type': 'service_worker', 'id': 'c', 'url': 'https://x.com/p?1'}]
        self.assertEqual(match_target(t, 'https://x.com/p?1')['id'], 'a')
        self.assertEqual(match_target(t, 'https://y.com/other')['id'], 'b')
        self.assertIsNone(match_target(t, 'https://z.com/'))


class StepTests(unittest.IsolatedAsyncioTestCase):
    async def test_never_takes_over_switches_and_stops_own_session(self):
        want = {'v': ('g1', 'https://x.com/1')}
        m = CastMirror({'cast': {'enabled': True, 'targets': {'tv': 'L'}}}, lambda d: want['v'])
        t = m.targets['tv']; calls = []
        async def call(method, **kw): calls.append(method)
        async def mirror(http, tid, sink): calls.append('mirror:' + tid)
        m._mirror = mirror
        class Resp:
            async def json(self): return [{'type': 'page', 'id': 'T1', 'url': 'https://x.com/1'}, {'type': 'page', 'id': 'T2', 'url': 'https://x.com/2?'}]
        class Http:
            async def get(self, url): return Resp()
        http = Http()
        m.sinks = {'L': {'name': 'L', 'session': 'YouTube'}}
        await m._step(http, call, t); self.assertEqual(calls, []); self.assertEqual(t.status['state'], 'busy')
        m.sinks = {'L': {'name': 'L', 'session': None}}
        await m._step(http, call, t); self.assertEqual(calls, ['mirror:T1'])
        m.sinks = {'L': {'name': 'L', 'session': 'Casting tab'}}
        await m._step(http, call, t); self.assertEqual(len(calls), 1)
        want['v'] = ('g2', 'https://x.com/2?')
        await m._step(http, call, t); self.assertEqual(calls[-1], 'Cast.stopCasting')
        m.sinks = {'L': {'name': 'L', 'session': None}}
        await m._step(http, call, t); self.assertEqual(calls[-1], 'mirror:T2')
        m.sinks = {'L': {'name': 'L', 'session': 'Casting tab'}}; await m._step(http, call, t)
        m.sinks = {'L': {'name': 'L', 'session': 'YouTube'}}  # someone cast over us
        await m._step(http, call, t); want['v'] = None; await m._step(http, call, t)
        self.assertEqual(calls[-1], 'mirror:T2')

    def test_disabled_without_targets(self):
        self.assertFalse(CastMirror({'cast': {'enabled': True}}, lambda d: None).enabled)
        self.assertTrue(CastMirror({'cast': {'enabled': True, 'sink': 'L'}}, lambda d: None).enabled)


if __name__ == '__main__':
    unittest.main()
