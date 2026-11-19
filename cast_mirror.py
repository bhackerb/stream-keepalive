"""Cast stream tabs to Google Cast devices (projector Chromecast, Nest Hub).

The server runs its player in a dedicated Google Chrome with the StreamKeeper
companion loaded. Each cast display (e.g. ``tv`` -> "Livingroom Cast",
``comm`` -> "The Comm") holds one stream; its tab is mirrored to the sink with
the CDP Cast domain, which carries the tab's audio (desktop mirroring does not
on Linux). A sink busy with someone else's session is never taken over, and
only sessions started here are stopped.

Config:
  cast:
    enabled: true
    targets: {tv: Livingroom Cast, comm: The Comm}
    chrome: google-chrome
    debug_port: 9333
    user_data_dir: ./cast-chrome
    extension: ./build/companion/chrome
    poll_seconds: 10
"""
import asyncio
import contextlib
import os
import time
from urllib.parse import urlparse

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


def match_target(targets: list[dict], url: str) -> dict | None:
    """Find the page target showing a stream URL; exact or owned-marker first, then a unique host."""
    pages = [t for t in targets if t.get('type') == 'page']
    exact = [t for t in pages if t.get('url', '').split('#')[0] == url.split('#')[0]]
    if len(exact) == 1:
        return exact[0]
    host = urlparse(url).hostname
    same = [t for t in pages if urlparse(t.get('url', '')).hostname == host]
    return same[0] if len(same) == 1 else None


class Target:
    """One cast display -> one sink."""

    def __init__(self, display, sink):
        self.display, self.sink = display, sink
        self.ours = False
        self.our_session = None
        self.stream = None  # (stream_id, url) currently mirrored
        self.target_id = None
        self.status = {'display': display, 'sink': sink, 'state': 'idle', 'message': None, 'stream_id': None, 'since': None}

    def set(self, state, message=None):
        if state != self.status['state']:
            self.status['since'] = int(time.time())
        self.status.update(state=state, message=message)


class CastMirror:
    def __init__(self, config: dict, wanted):
        self.cfg = config.get('cast', {}) or {}
        targets = self.cfg.get('targets') or ({self.cfg.get('display', 'tv'): self.cfg['sink']} if self.cfg.get('sink') else {})
        self.targets = {d: Target(d, s) for d, s in targets.items() if isinstance(s, str) and s}
        self.enabled = bool(self.cfg.get('enabled')) and bool(self.targets)
        self.port = int(self.cfg.get('debug_port', 9333))
        self.wanted = wanted  # callable(display) -> (stream_id, url) | None
        self.proc = None
        self.task = None
        self.sinks: dict[str, dict] = {}
        self.state = 'disabled' if not self.enabled else 'starting'
        self.message = None

    def snapshot(self):
        return {'enabled': self.enabled, 'state': self.state, 'message': self.message,
                'targets': {d: dict(t.status) for d, t in self.targets.items()}}

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

    async def _launch_chrome(self, http):
        with contextlib.suppress(Exception):
            await (await http.get(f'http://127.0.0.1:{self.port}/json/version')).json()
            return  # already running (ours from before a restart, or still alive)
        args = [self.cfg.get('chrome', 'google-chrome'), f'--remote-debugging-port={self.port}',
                f'--user-data-dir={os.path.abspath(self.cfg.get("user_data_dir", "./cast-chrome"))}',
                '--enable-unsafe-extension-debugging', '--autoplay-policy=no-user-gesture-required',
                '--no-first-run', '--no-default-browser-check', '--disable-session-crashed-bubble',
                '--hide-crash-restore-bubble', '--disable-background-timer-throttling',
                '--disable-renderer-backgrounding', '--disable-backgrounding-occluded-windows',
                '--window-position=-3000,-3000', '--window-size=200,200', 'about:blank']
        # Chrome with a custom profile looks for native hosts inside that profile.
        profile = os.path.abspath(self.cfg.get('user_data_dir', './cast-chrome'))
        hosts = os.path.join(profile, 'NativeMessagingHosts')
        source = os.path.expanduser('~/.config/google-chrome/NativeMessagingHosts')
        if os.path.isdir(source):
            os.makedirs(hosts, exist_ok=True)
            for name in os.listdir(source):
                with open(os.path.join(source, name), 'rb') as f, open(os.path.join(hosts, name), 'wb') as g:
                    g.write(f.read())
        self.proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL,
                                                         stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        await asyncio.sleep(4)
        version = await (await http.get(f'http://127.0.0.1:{self.port}/json/version')).json()
        extension = os.path.abspath(self.cfg.get('extension', './build/companion/chrome'))
        async with http.ws_connect(version['webSocketDebuggerUrl'], max_msg_size=0) as ws:
            await ws.send_json({'id': 1, 'method': 'Extensions.loadUnpacked', 'params': {'path': extension}})
            reply = await asyncio.wait_for(ws.receive_json(), 15)
            if 'error' in reply:
                raise RuntimeError('Companion load failed: ' + str(reply['error'].get('message')))

    async def _run(self):
        while True:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # Chrome died, sink vanished, CDP closed: retry calmly.
                for t in self.targets.values():
                    t.ours, t.target_id = False, None
                self.state, self.message = 'error', str(e)[:200]
            await asyncio.sleep(15)

    async def _cdp(self, http, ws_url):
        """Open a CDP connection; returns (call, close, reader_task)."""
        ws = await http.ws_connect(ws_url, max_msg_size=0)
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
                        if 'error' in m:
                            fut.set_exception(RuntimeError(m['error'].get('message')))
                        else:
                            fut.set_result(m.get('result'))
                elif m.get('method') == 'Cast.sinksUpdated':
                    self.sinks = {s['name']: s for s in m['params']['sinks']}
                elif m.get('method') == 'Cast.issueUpdated' and m['params'].get('issueMessage'):
                    self.message = m['params']['issueMessage'][:200]
            raise ConnectionError('Cast helper disconnected')

        return call, ws.close, asyncio.create_task(reader())

    async def _session(self):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as http:
            await self._launch_chrome(http)
            tabs = await (await http.get(f'http://127.0.0.1:{self.port}/json')).json()
            # A long-lived about:blank watcher keeps sink discovery running.
            watcher = next((t for t in tabs if t.get('type') == 'page' and t.get('url') == 'about:blank'), None)
            if watcher is None:
                watcher = await (await http.put(f'http://127.0.0.1:{self.port}/json/new?about:blank')).json()
            call, close, read = await self._cdp(http, watcher['webSocketDebuggerUrl'])
            try:
                await call('Cast.enable')
                self.state, self.message = 'running', None
                while not read.done():
                    for t in self.targets.values():
                        try:
                            await self._step(http, call, t)
                        except Exception as e:
                            t.set('error', str(e)[:200])
                    await asyncio.sleep(float(self.cfg.get('poll_seconds', 10)))
                read.result()
            finally:
                read.cancel()
                await close()

    async def _mirror(self, http, target_id, sink):
        """Start tab mirroring from the stream's own page target."""
        tabs = await (await http.get(f'http://127.0.0.1:{self.port}/json')).json()
        page = next(t for t in tabs if t.get('id') == target_id)
        call, close, read = await self._cdp(http, page['webSocketDebuggerUrl'])
        try:
            await call('Cast.enable')
            for _ in range(10):  # the page's own sink list fills after enable
                await asyncio.sleep(1)
                with contextlib.suppress(Exception):
                    await call('Cast.startTabMirroring', sinkName=sink)
                    return
            await call('Cast.startTabMirroring', sinkName=sink)
        finally:
            read.cancel()
            await close()

    async def _step(self, http, call, t: Target):
        sink = self.sinks.get(t.sink)
        want = self.wanted(t.display)
        if t.ours and sink:
            session = sink.get('session')
            if not session:
                t.ours, t.stream = False, None  # ended on the device side
            elif t.our_session is None:
                t.our_session = session
            elif session != t.our_session:
                t.ours, t.stream = False, None  # someone else cast over us; leave them be
        if t.ours and want and t.stream and want[0] != t.stream[0]:
            # A different game took this display: stop ours, restart on the new tab next poll.
            await call('Cast.stopCasting', sinkName=t.sink)
            t.ours, t.stream, t.target_id = False, None, None
            t.set('switching')
            return
        action = decide(bool(want), sink, t.ours)
        if action == 'start':
            tabs = await (await http.get(f'http://127.0.0.1:{self.port}/json')).json()
            page = match_target(tabs, want[1])
            if page is None:
                t.set('waiting', 'Stream tab not found yet')
                return
            await self._mirror(http, page['id'], t.sink)
            t.ours, t.our_session, t.stream, t.target_id = True, None, want, page['id']
            t.status['stream_id'] = want[0]
            t.set('casting')
        elif action == 'stop':
            await call('Cast.stopCasting', sinkName=t.sink)
            t.ours, t.stream, t.target_id = False, None, None
            t.status['stream_id'] = None
            t.set('idle')
        elif action == 'yield':
            t.set('busy', f'{t.sink} is in use ({sink.get("session")}); not taking over')
        elif sink is None:
            t.set('searching', f'{t.sink} not discovered yet')
        elif not want and not t.ours:
            t.status['stream_id'] = None
            t.set('idle')
