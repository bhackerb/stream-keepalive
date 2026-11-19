"""Mirror the server's TV desktop to a Google Cast device (e.g. a projector Chromecast).

The Firefox player keeps owning playback on the X display; a separate, tiny
Chrome instance only casts that desktop through the DevTools Cast domain.
It never takes over a sink that is busy with someone else's session.

Config (all optional except enabled/sink):
  cast:
    enabled: true
    sink: Livingroom Cast
    display: tv              # layout display whose streams trigger casting
    chrome: google-chrome
    debug_port: 9333
    user_data_dir: ./cast-chrome
    poll_seconds: 10
"""
import asyncio
import contextlib
import os
import time

import aiohttp


def decide(desired: bool, sink: dict | None, ours: bool) -> str:
    """Pure policy: 'start', 'stop', 'yield' or 'none'.

    sink is {'name', 'session'} from Cast.sinksUpdated (None when not discovered).
    ours is True when this helper started the session currently on the sink.
    """
    if sink is None:
        return 'none'
    busy = bool(sink.get('session'))
    if desired:
        if not busy:
            return 'start'
        return 'none' if ours else 'yield'
    return 'stop' if ours and busy else 'none'


class CastMirror:
    def __init__(self, config: dict, wanted):
        self.cfg = config.get('cast', {}) or {}
        self.enabled = bool(self.cfg.get('enabled')) and bool(self.cfg.get('sink'))
        self.sink_name = self.cfg.get('sink')
        self.display = self.cfg.get('display', 'tv')
        self.port = int(self.cfg.get('debug_port', 9333))
        self.wanted = wanted  # callable(display) -> bool: any live stream on that display
        self.proc = None
        self.task = None
        self.sinks: dict[str, dict] = {}
        self.ours = False
        self.our_session = None
        self.status = {'enabled': self.enabled, 'sink': self.sink_name, 'state': 'disabled' if not self.enabled else 'starting',
                       'message': None, 'since': None}

    def snapshot(self):
        return dict(self.status)

    def _set(self, state, message=None):
        if state != self.status['state']:
            self.status['since'] = int(time.time())
        self.status.update(state=state, message=message)

    async def start(self):
        if self.enabled:
            self.task = asyncio.create_task(self._run())

    async def shutdown(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.proc.wait(), 5)

    async def _launch_chrome(self):
        if self.proc and self.proc.returncode is None:
            return
        args = [self.cfg.get('chrome', 'google-chrome'), f'--remote-debugging-port={self.port}',
                f'--user-data-dir={os.path.abspath(self.cfg.get("user_data_dir", "./cast-chrome"))}',
                '--no-first-run', '--no-default-browser-check', '--disable-session-crashed-bubble',
                '--auto-select-desktop-capture-source=Entire screen', '--auto-accept-this-tab-capture',
                # Keep the helper window off the mirrored desktop.
                '--window-position=-3000,-3000', '--window-size=200,200', 'about:blank']
        self.proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL,
                                                         stderr=asyncio.subprocess.DEVNULL)
        await asyncio.sleep(3)

    async def _run(self):
        while True:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # Chrome died, sink vanished, CDP closed: retry calmly.
                self.ours = False
                self._set('error', str(e)[:200])
            await asyncio.sleep(15)

    async def _session(self):
        await self._launch_chrome()
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as http:
            tabs = await (await http.get(f'http://127.0.0.1:{self.port}/json')).json()
            page = next(t for t in tabs if t.get('type') == 'page')
            async with http.ws_connect(page['webSocketDebuggerUrl'], max_msg_size=0) as ws:
                ids = iter(range(1, 1 << 30))
                pending: dict[int, asyncio.Future] = {}

                async def call(method, **params):
                    i = next(ids)
                    fut = asyncio.get_running_loop().create_future()
                    pending[i] = fut
                    await ws.send_json({'id': i, 'method': method, 'params': params})
                    return await asyncio.wait_for(fut, 15)

                async def reader():
                    async for msg in ws:
                        m = msg.json()
                        if 'id' in m and m['id'] in pending:
                            fut = pending.pop(m['id'])
                            if not fut.done():
                                fut.set_exception(RuntimeError(m['error'].get('message'))) if 'error' in m else fut.set_result(m.get('result'))
                        elif m.get('method') == 'Cast.sinksUpdated':
                            self.sinks = {s['name']: s for s in m['params']['sinks']}
                        elif m.get('method') == 'Cast.issueUpdated' and m['params'].get('issueMessage'):
                            self.status['message'] = m['params']['issueMessage'][:200]
                    raise ConnectionError('Cast helper disconnected')

                read = asyncio.create_task(reader())
                try:
                    await call('Cast.enable')
                    self._set('searching')
                    while not read.done():
                        await self._step(call)
                        await asyncio.sleep(float(self.cfg.get('poll_seconds', 10)))
                    read.result()
                finally:
                    read.cancel()

    async def _step(self, call):
        sink = self.sinks.get(self.sink_name)
        desired = bool(self.wanted(self.display))
        if self.ours and sink:
            session = sink.get('session')
            if not session:
                self.ours = False  # session ended on the device side
            elif self.our_session is None:
                self.our_session = session
            elif session != self.our_session:
                self.ours = False  # someone else cast over us; leave them be
        action = decide(desired, sink, self.ours)
        if action == 'start':
            await call('Cast.startDesktopMirroring', sinkName=self.sink_name)
            self.ours, self.our_session = True, None
            self._set('casting')
        elif action == 'stop':
            await call('Cast.stopCasting', sinkName=self.sink_name)
            self.ours = False
            self._set('idle')
        elif action == 'yield':
            self._set('busy', f'{self.sink_name} is in use ({sink.get("session")}); not taking over')
        elif sink is None:
            self._set('searching', f'{self.sink_name} not discovered yet')
        elif not desired and not self.ours:
            self._set('idle')
