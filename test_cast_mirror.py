import unittest
from cast_mirror import CastMirror, decide


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


class StepTests(unittest.IsolatedAsyncioTestCase):
    async def test_never_takes_over_and_stops_own_session(self):
        want = {'v': True}
        m = CastMirror({'cast': {'enabled': True, 'sink': 'L'}}, lambda d: want['v'])
        calls = []
        async def call(method, **kw): calls.append(method)
        m.sinks = {'L': {'name': 'L', 'session': 'YouTube'}}
        await m._step(call); self.assertEqual(calls, []); self.assertEqual(m.status['state'], 'busy')
        m.sinks = {'L': {'name': 'L', 'session': None}}
        await m._step(call); self.assertEqual(calls, ['Cast.startDesktopMirroring'])
        m.sinks = {'L': {'name': 'L', 'session': 'Casting screen'}}
        await m._step(call); self.assertEqual(len(calls), 1)
        m.sinks = {'L': {'name': 'L', 'session': 'YouTube'}}  # someone cast over us
        await m._step(call); want['v'] = False; await m._step(call)
        self.assertEqual(calls, ['Cast.startDesktopMirroring'])
        m.sinks = {'L': {'name': 'L', 'session': None}}; want['v'] = True
        await m._step(call); m.sinks = {'L': {'name': 'L', 'session': 'Casting screen'}}
        await m._step(call); want['v'] = False; await m._step(call)
        self.assertEqual(calls[-1], 'Cast.stopCasting')

    def test_disabled_without_sink(self):
        self.assertFalse(CastMirror({'cast': {'enabled': True}}, lambda d: True).enabled)


if __name__ == '__main__':
    unittest.main()
