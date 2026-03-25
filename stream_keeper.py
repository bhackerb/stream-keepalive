"""
StreamKeeper — Main orchestrator.

Ties together:
- Playwright browser (headed, with uBlock Origin)
- Site drivers (streamed.pk, onhockey.tv)
- Health monitoring loop (per-stream)
- Recovery cascade (per-stream)
- Discord bot interface

Supports multiple simultaneous streams in separate browser tabs.
"""

import asyncio
import hashlib
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

try:
    import discord
    from discord.ext import commands
    HAS_DISCORD = True
except ImportError:
    HAS_DISCORD = False
    print("⚠️  discord.py not installed — running in CLI-only mode")
    print("   Install with: pip install discord.py --break-system-packages")

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from ad_handler import AdHandler
from api_server import StreamKeeperAPI
from health_monitor import HealthMonitor, StreamState
from sites import get_driver, BaseSiteDriver

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging(config: dict):
    log_cfg = config.get("logging", {})
    level = getattr(logging, log_cfg.get("level", "INFO").upper(), logging.INFO)
    log_file = log_cfg.get("file", "./logs/stream-keeper.log")

    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file),
        ],
    )

logger = logging.getLogger("stream-keeper")

# ---------------------------------------------------------------------------
# ActiveStream dataclass
# ---------------------------------------------------------------------------

@dataclass
class ActiveStream:
    """Represents a single active stream with its own tab, driver, and health monitor."""
    id: str                              # e.g. "blues", "cardinals" — team name lowered
    team: str                            # display name
    page: Page                           # browser tab
    driver: Optional[BaseSiteDriver]     # site driver (None for direct URL watches)
    health_monitor: HealthMonitor
    monitor_task: Optional[asyncio.Task] = None
    site: str = ""
    url: Optional[str] = None            # for direct URL watches
    started_at: float = field(default_factory=time.time)
    recovery_in_progress: bool = False

# ---------------------------------------------------------------------------
# StreamKeeper core
# ---------------------------------------------------------------------------

class StreamKeeper:
    """Core orchestrator for stream monitoring and recovery."""

    def __init__(self, config: dict):
        self.config = config
        self.ad_handler = AdHandler(config)
        self.context: Optional[BrowserContext] = None
        self._browser: Optional[Browser] = None
        self._cdp_url: Optional[str] = None
        self.active_streams: dict[str, ActiveStream] = {}
        self.default_site: str = config.get("defaults", {}).get("site", "streamed.pk")
        self._playwright = None

        # Limits
        self.max_streams = config.get("streams", {}).get("max_streams", 8)

        # Recovery settings
        health_cfg = config.get("health", {})
        self.max_recovery_attempts = health_cfg.get("max_recovery_attempts", 5)
        self.recovery_cooldown = health_cfg.get("recovery_cooldown_seconds", 30)
        self.screenshot_on_failure = health_cfg.get("screenshot_on_failure", True)

    # ------------------------------------------------------------------
    # Backward-compat properties — point at first stream or sensible defaults
    # ------------------------------------------------------------------

    @property
    def is_watching(self) -> bool:
        return len(self.active_streams) > 0

    @property
    def current_team(self) -> Optional[str]:
        if not self.active_streams:
            return None
        # Return the first stream's team
        return next(iter(self.active_streams.values())).team

    @property
    def current_site(self) -> str:
        if not self.active_streams:
            return self.default_site
        return next(iter(self.active_streams.values())).site or self.default_site

    @property
    def health_monitor(self) -> HealthMonitor:
        """Return the first stream's health monitor for backward compat."""
        if self.active_streams:
            return next(iter(self.active_streams.values())).health_monitor
        # Return a dummy monitor if nothing is active
        return HealthMonitor(self.config)

    @property
    def page(self) -> Optional[Page]:
        """Return the first stream's page for backward compat."""
        if self.active_streams:
            return next(iter(self.active_streams.values())).page
        return None

    @property
    def driver(self) -> Optional[BaseSiteDriver]:
        """Return the first stream's driver for backward compat."""
        if self.active_streams:
            return next(iter(self.active_streams.values())).driver
        return None

    # ------------------------------------------------------------------
    # Stream ID generation
    # ------------------------------------------------------------------

    def _make_stream_id(self, team: Optional[str] = None, url: Optional[str] = None) -> str:
        """Generate a stream ID from team name or URL."""
        if team:
            base_id = team.lower().replace(" ", "-")
        elif url:
            base_id = hashlib.md5(url.encode()).hexdigest()[:8]
        else:
            base_id = f"stream-{int(time.time())}"

        # Deduplicate if ID already exists
        stream_id = base_id
        counter = 2
        while stream_id in self.active_streams:
            stream_id = f"{base_id}-{counter}"
            counter += 1
        return stream_id

    # ------------------------------------------------------------------
    # Browser management
    # ------------------------------------------------------------------

    async def start_browser(self):
        """Launch the Playwright browser with uBlock Origin."""
        browser_cfg = self.config.get("browser", {})
        user_data_dir = browser_cfg.get("user_data_dir", "./browser-data")
        ublock_path = browser_cfg.get("ublock_origin_path", "./extensions/ublock-origin")
        viewport_w = browser_cfg.get("viewport_width", 1920)
        viewport_h = browser_cfg.get("viewport_height", 1080)
        extra_args = browser_cfg.get("extra_args", [])

        os.makedirs(user_data_dir, exist_ok=True)

        self._playwright = await async_playwright().start()

        # Build launch args
        args = [
            "--autoplay-policy=no-user-gesture-required",
            "--disable-features=PreloadMediaEngagementData,MediaEngagementBypassAutoplayPolicies",
        ] + extra_args

        # Add uBlock Origin if the path exists
        ublock_abs = str(Path(ublock_path).resolve())
        if Path(ublock_path).exists() and (Path(ublock_path) / "manifest.json").exists():
            args.extend([
                f"--disable-extensions-except={ublock_abs}",
                f"--load-extension={ublock_abs}",
            ])
            logger.info(f"Loading uBlock Origin from: {ublock_abs}")
        else:
            logger.warning(
                f"uBlock Origin not found at {ublock_path} — "
                "running with network-level ad blocking only"
            )

        # Headless mode: useful for server-side health monitoring without a display
        headless = browser_cfg.get("headless", False)
        channel = browser_cfg.get("channel", "chromium")
        remote_debugging_url = browser_cfg.get("remote_debugging_url")

        cdp_connected = False
        if remote_debugging_url:
            # Try to connect to an already-running Chrome via CDP
            try:
                import aiohttp as _aiohttp_check
                async with _aiohttp_check.ClientSession() as sess:
                    async with sess.get(f"{remote_debugging_url.rstrip('/')}/json/version", timeout=_aiohttp_check.ClientTimeout(total=2)) as r:
                        if r.status == 200:
                            self._cdp_url = remote_debugging_url
                            logger.info(f"Connecting to existing browser at {remote_debugging_url}")
                            self._browser = await self._playwright.chromium.connect_over_cdp(remote_debugging_url)
                            if self._browser.contexts:
                                self.context = self._browser.contexts[0]
                            else:
                                self.context = await self._browser.new_context()
                            page_count = len(self.context.pages) if self.context else 0
                            logger.info(f"CDP connected — {page_count} existing tab(s) found")
                            cdp_connected = True
                        else:
                            logger.warning(f"CDP endpoint returned {r.status} — falling back to headless browser")
            except Exception as e:
                logger.warning(f"Stream Chrome not running at {remote_debugging_url} ({e}) — falling back to headless browser")

        if not cdp_connected:
            # Launch a new persistent context
            self.context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir,
                headless=headless,
                channel=channel,
                args=args,
                viewport={"width": viewport_w, "height": viewport_h},
                ignore_default_args=["--disable-extensions"],
                permissions=["geolocation"],
            )

        # Set up ad blocking on the context
        await self.ad_handler.setup_network_blocking(self.context)

        # Handle popup windows (ad popups)
        self.context.on("page", self._on_new_page)

        logger.info("Browser started successfully")

    async def _reconnect_cdp(self):
        """Reconnect to Chrome via CDP to pick up newly opened tabs.

        Playwright's CDP connection doesn't dynamically detect tabs opened
        by the user through the browser UI. Reconnecting gets a fresh view.
        """
        if not self._cdp_url:
            return
        logger.debug("Reconnecting CDP to refresh tab list...")
        try:
            if self._browser:
                await self._browser.close()
        except Exception as e:
            logger.debug(f"CDP disconnect (expected): {e}")

        try:
            self._browser = await self._playwright.chromium.connect_over_cdp(self._cdp_url)
            contexts = self._browser.contexts
            if contexts:
                self.context = contexts[0]
            else:
                self.context = await self._browser.new_context()
            page_count = len(self.context.pages) if self.context else 0
            logger.debug(f"CDP reconnected — {page_count} tab(s)")
        except Exception as e:
            logger.error(f"CDP reconnect failed: {e}")

    async def _on_new_page(self, page: Page):
        """Handle new pages (popup ad windows).

        Only auto-close pages that aren't tracked as active streams.
        """
        await asyncio.sleep(1)  # Let it load briefly

        # Check if this page belongs to an active stream (we created it)
        for stream in self.active_streams.values():
            if stream.page is page:
                return  # It's one of ours, don't touch it

        handled = await self.ad_handler.handle_new_page_popup(page)
        if not handled:
            logger.info(f"New page opened: {page.url[:80]}")

    async def _new_stream_page(self) -> Page:
        """Create a new browser tab for a stream."""
        if not self.context:
            await self.start_browser()
        return await self.context.new_page()

    # ------------------------------------------------------------------
    # watch / watch_url — multi-stream versions
    # ------------------------------------------------------------------

    async def watch_url(self, url: str, label: Optional[str] = None) -> str:
        """Start watching a stream at a direct URL in a new tab."""
        if len(self.active_streams) >= self.max_streams:
            return f"❌ Max streams ({self.max_streams}) reached. Stop one first with `!stop <name>`."

        if not self.context:
            await self.start_browser()

        team = label or url.split("/")[-1][:40]
        stream_id = self._make_stream_id(team=team if label else None, url=url)
        logger.info(f"Starting direct watch [{stream_id}]: {team} at {url}")

        page = await self._new_stream_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            await page.close()
            return f"❌ Failed to navigate to URL: {e}"

        # Aggressive startup: multiple rounds of overlay dismissal + video detection
        # Streaming sites often have layered ads that appear after initial load
        has_video = False
        for attempt in range(5):
            await asyncio.sleep(3 if attempt == 0 else 2)

            # Dismiss overlays
            try:
                dismissed = await self.ad_handler.dismiss_overlays(page)
                if dismissed > 0:
                    logger.info(f"[{stream_id}] Startup round {attempt+1}: dismissed {dismissed} overlays")
            except Exception:
                pass

            # Click any visible play buttons or stream source links
            try:
                await page.evaluate("""() => {
                    // Click play buttons
                    const playBtns = document.querySelectorAll(
                        'button[class*="play"], [class*="play-btn"], [class*="play-button"],' +
                        'div[class*="play"], svg[class*="play"], [aria-label*="play" i],' +
                        '.vjs-big-play-button, .jw-icon-playback, [data-plyr="play"]'
                    );
                    for (const btn of playBtns) {
                        if (btn.offsetParent !== null) { btn.click(); break; }
                    }
                    // Click stream source links (first available)
                    const sources = document.querySelectorAll(
                        'a[href*="stream"], a[href*="player"], .source-link, .stream-link'
                    );
                    for (const src of sources) {
                        if (src.offsetParent !== null) { src.click(); break; }
                    }
                }""")
            except Exception:
                pass

            # Check for video element
            has_video = await page.evaluate("""() => {
                let video = document.querySelector('video');
                if (!video) {
                    const iframes = document.querySelectorAll('iframe');
                    for (const iframe of iframes) {
                        try {
                            const doc = iframe.contentDocument || iframe.contentWindow?.document;
                            if (doc) { video = doc.querySelector('video'); if (video) break; }
                        } catch (e) {}
                    }
                }
                if (!video) return false;
                video.muted = false;
                video.volume = 1.0;
                video.play().catch(() => {});
                return true;
            }""")

            if has_video:
                logger.info(f"[{stream_id}] Video found after {attempt+1} startup rounds")
                break

        # Create ActiveStream
        health_mon = HealthMonitor(self.config)
        health_mon.history.recovery_count = 0
        stream = ActiveStream(
            id=stream_id,
            team=team,
            page=page,
            driver=None,
            health_monitor=health_mon,
            site="direct",
            url=url,
        )
        stream.monitor_task = asyncio.create_task(self._monitor_loop(stream))
        self.active_streams[stream_id] = stream

        if has_video:
            return (
                f"📺 Now watching: **{team}** [`{stream_id}`]\n"
                f"🔗 URL: {url[:60]}...\n"
                f"🛡️ Ad blocking active | Health monitoring started\n"
                f"📊 Active streams: {len(self.active_streams)}/{self.max_streams}"
            )
        else:
            return (
                f"⚠️ Navigated to **{team}** [`{stream_id}`] but no video element found yet.\n"
                f"🔗 URL: {url[:60]}...\n"
                f"🛡️ Health monitoring started — will detect video when it loads\n"
                f"📊 Active streams: {len(self.active_streams)}/{self.max_streams}"
            )

    def _get_cdp_base(self) -> str:
        """Get the CDP base URL for /json/list queries."""
        remote_url = self.config.get("browser", {}).get("remote_debugging_url", "http://localhost:9222")
        return remote_url.rstrip("/")

    async def _get_iframe_targets_for_page(self, page_url: str) -> list[dict]:
        """Get iframe CDP targets belonging to a specific page URL."""
        import aiohttp as _aiohttp
        try:
            async with _aiohttp.ClientSession() as sess:
                async with sess.get(f"{self._get_cdp_base()}/json/list") as r:
                    targets = await r.json()
        except Exception:
            return []

        cur_page_url = None
        iframes: list[dict] = []
        for t in targets:
            if t.get("type") == "page":
                cur_page_url = t.get("url", "")
            elif t.get("type") == "iframe" and cur_page_url:
                # Match by exact URL or by common path prefix (handles URL changes)
                if cur_page_url == page_url or (page_url and page_url.split("?")[0] in cur_page_url):
                    iframes.append(t)
        return iframes

    async def _check_video_health_cdp(self, page_url: str) -> dict | None:
        """Use CDP to check video health through cross-origin iframes."""
        iframes = await self._get_iframe_targets_for_page(page_url)
        if not iframes:
            return None

        js_check = """(() => {
            const v = document.querySelector('video');
            if (!v) return null;
            return {
                readyState: v.readyState,
                currentTime: v.currentTime,
                paused: v.paused,
                ended: v.ended,
                muted: v.muted,
                volume: v.volume,
                videoWidth: v.videoWidth,
                videoHeight: v.videoHeight,
                networkState: v.networkState,
                error: v.error ? {code: v.error.code, message: v.error.message} : null,
                bufferedEnd: v.buffered && v.buffered.length > 0 ? v.buffered.end(v.buffered.length - 1) : 0
            };
        })()"""

        import aiohttp as _aiohttp
        async with _aiohttp.ClientSession() as sess:
            for iframe in iframes:
                ws_url = iframe.get("webSocketDebuggerUrl")
                if not ws_url:
                    continue
                try:
                    async with sess.ws_connect(ws_url) as ws:
                        await ws.send_json({
                            "id": 1, "method": "Runtime.evaluate",
                            "params": {"expression": js_check, "returnByValue": True}
                        })
                        resp = await asyncio.wait_for(ws.receive_json(), timeout=5)
                        result = resp.get("result", {}).get("result", {}).get("value")
                        if result and isinstance(result, dict) and "readyState" in result:
                            return result
                except Exception:
                    continue
        return None

    async def _force_play_cdp(self, page_url: str) -> bool:
        """Use CDP to force play + unmute the video through cross-origin iframes.

        Uses Input.dispatchMouseEvent (simulated click) to satisfy browser autoplay policy.
        """
        iframes = await self._get_iframe_targets_for_page(page_url)

        import aiohttp as _aiohttp
        for iframe in iframes:
            ws_url = iframe.get("webSocketDebuggerUrl")
            if not ws_url:
                continue
            try:
                async with _aiohttp.ClientSession() as sess:
                    async with sess.ws_connect(ws_url) as ws:
                        # Get video element position
                        await ws.send_json({
                            "id": 1, "method": "Runtime.evaluate",
                            "params": {
                                "expression": "(function(){ var v = document.querySelector('video'); if (!v) return null; var r = v.getBoundingClientRect(); return {x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2), w: r.width}; })()",
                                "returnByValue": True
                            }
                        })
                        resp = await asyncio.wait_for(ws.receive_json(), timeout=3)
                        pos = resp.get("result", {}).get("result", {}).get("value")

                        if pos and pos.get("w", 0) > 0:
                            # Simulated click (user gesture) to start playback
                            x, y = pos["x"], pos["y"]
                            await ws.send_json({"id": 2, "method": "Input.dispatchMouseEvent", "params": {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1}})
                            await asyncio.wait_for(ws.receive_json(), timeout=3)
                            await ws.send_json({"id": 3, "method": "Input.dispatchMouseEvent", "params": {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1}})
                            await asyncio.wait_for(ws.receive_json(), timeout=3)

                            # Unmute
                            await asyncio.sleep(1)
                            await ws.send_json({
                                "id": 4, "method": "Runtime.evaluate",
                                "params": {
                                    "expression": "(function(){ var v = document.querySelector('video'); if(!v) return false; v.muted=false; v.volume=1.0; try{jwplayer().setMute(false);jwplayer().setVolume(100);}catch(e){} return true; })()",
                                    "returnByValue": True
                                }
                            })
                            await asyncio.wait_for(ws.receive_json(), timeout=3)
                            logger.info(f"CDP force play (click) on iframe: {iframe.get('url', '')[:60]}")
                            return True
                        else:
                            # Fallback: try JS play (may not work with autoplay policy)
                            await ws.send_json({
                                "id": 5, "method": "Runtime.evaluate",
                                "params": {
                                    "expression": "(() => { const v = document.querySelector('video'); if (!v) return false; v.play().catch(()=>{}); v.muted = false; v.volume = 1.0; return true; })()",
                                    "returnByValue": True
                                }
                            })
                            resp = await asyncio.wait_for(ws.receive_json(), timeout=3)
                            val = resp.get("result", {}).get("result", {}).get("value", False)
                            if val:
                                logger.info(f"CDP force play (JS) on iframe: {iframe.get('url', '')[:60]}")
                                return True
            except Exception:
                continue
        return False

    async def _auto_setup_stream_cdp(self, page_url: str) -> bool:
        """CDP-based auto-setup: click play on JW Player, activate theater mode on streamed.pk.

        Handles the full chain:
        1. Find the pooembed.eu iframe (JW Player) and click play + unmute
        2. Enable theater mode on the parent streamed.pk watch page
        """
        cdp_base = self._get_cdp_base()
        import aiohttp as _aiohttp

        try:
            async with _aiohttp.ClientSession() as sess:
                async with sess.get(f"{cdp_base}/json/list") as r:
                    targets = await r.json()
        except Exception:
            return False

        did_something = False

        # Step 1: Find and click play on JW Player iframes (pooembed.eu or similar)
        for t in targets:
            if t.get("type") != "iframe":
                continue
            iframe_url = t.get("url", "")
            ws_url = t.get("webSocketDebuggerUrl")
            if not ws_url:
                continue

            # Only target iframes that are related to our page URL
            # JW Player lives in pooembed.eu, embedhd.org, or similar
            if not any(domain in iframe_url for domain in ["pooembed", "embedhd", "embedme", "sportshd"]):
                continue

            try:
                async with _aiohttp.ClientSession() as sess2:
                    async with sess2.ws_connect(ws_url) as ws:
                        # Click JW Player play button and unmute
                        js_play = """(() => {
                            var v = document.querySelector('video');
                            if (!v) return 'no-video';
                            var jwp = document.querySelector('.jwplayer');
                            if (jwp) {
                                // JW Player API
                                var api = jwplayer();
                                if (api && api.play) { api.play(); api.setMute(false); api.setVolume(100); return 'jw-play'; }
                            }
                            // Fallback: direct video element
                            v.play().catch(function(){});
                            v.muted = false;
                            v.volume = 1.0;
                            return 'direct-play';
                        })()"""
                        await ws.send_json({
                            "id": 1, "method": "Runtime.evaluate",
                            "params": {"expression": js_play, "returnByValue": True}
                        })
                        resp = await asyncio.wait_for(ws.receive_json(), timeout=5)
                        val = resp.get("result", {}).get("result", {}).get("value", "")
                        if val and val != "no-video":
                            logger.info(f"Auto-play via CDP ({val}): {iframe_url[:60]}")
                            did_something = True
            except Exception:
                continue

        # Step 2: Enable theater mode on the streamed.pk watch page
        for t in targets:
            if t.get("type") != "page":
                continue
            target_url = t.get("url", "")
            if "streamed.pk/watch/" not in target_url:
                continue
            ws_url = t.get("webSocketDebuggerUrl")
            if not ws_url:
                continue

            try:
                async with _aiohttp.ClientSession() as sess2:
                    async with sess2.ws_connect(ws_url) as ws:
                        # Click theater mode button if not already active
                        js_theater = """(() => {
                            // streamed.pk theater mode: look for expand/theater button
                            var btns = document.querySelectorAll('button, [role=button]');
                            for (var i = 0; i < btns.length; i++) {
                                var b = btns[i];
                                var text = (b.innerText || b.textContent || '').toLowerCase();
                                var cls = (b.className || '').toLowerCase();
                                var title = (b.getAttribute('title') || '').toLowerCase();
                                if (text.includes('theater') || text.includes('theatre') ||
                                    cls.includes('theater') || cls.includes('expand') ||
                                    title.includes('theater') || title.includes('expand')) {
                                    b.click();
                                    return 'theater-clicked';
                                }
                            }
                            // Try aria-label
                            var theater = document.querySelector('[aria-label*="theater" i], [aria-label*="expand" i]');
                            if (theater) { theater.click(); return 'theater-aria'; }
                            return 'no-theater-btn';
                        })()"""
                        await ws.send_json({
                            "id": 1, "method": "Runtime.evaluate",
                            "params": {"expression": js_theater, "returnByValue": True}
                        })
                        resp = await asyncio.wait_for(ws.receive_json(), timeout=5)
                        val = resp.get("result", {}).get("result", {}).get("value", "")
                        if val and "theater" in val:
                            logger.info(f"Theater mode activated: {val}")
                            did_something = True
                        else:
                            logger.debug(f"Theater mode: {val}")
            except Exception:
                continue

        return did_something

    async def _get_urls_with_video_cdp(self) -> set:
        """Query CDP iframe targets directly to find which page URLs have video players.

        Handles doubly-nested cross-origin iframes that Playwright can't traverse.
        Uses the CDP /json/list ordering: iframe targets follow their parent page target.
        """
        remote_url = self.config.get("browser", {}).get("remote_debugging_url", "http://localhost:9222")
        cdp_base = remote_url.rstrip("/")

        import aiohttp as _aiohttp
        try:
            async with _aiohttp.ClientSession() as sess:
                async with sess.get(f"{cdp_base}/json/list") as r:
                    targets = await r.json()
        except Exception:
            return set()

        # Group: iframes that appear between two page entries belong to the preceding page
        groups: list[tuple[dict, list[dict]]] = []
        cur_page: dict | None = None
        cur_iframes: list[dict] = []
        for t in targets:
            if t.get("type") == "page":
                if cur_page:
                    groups.append((cur_page, cur_iframes))
                cur_page, cur_iframes = t, []
            elif t.get("type") == "iframe" and cur_page:
                cur_iframes.append(t)
        if cur_page:
            groups.append((cur_page, cur_iframes))

        pages_with_video: set[str] = set()
        async with _aiohttp.ClientSession() as sess:
            for page_target, iframes in groups:
                for iframe in iframes:
                    ws_url = iframe.get("webSocketDebuggerUrl")
                    if not ws_url:
                        continue
                    try:
                        async with sess.ws_connect(ws_url) as ws:
                            await ws.send_json({"id": 1, "method": "Runtime.evaluate",
                                               "params": {"expression": "!!document.querySelector('video')",
                                                          "returnByValue": True}})
                            resp = await asyncio.wait_for(ws.receive_json(), timeout=3)
                            has_vid = resp.get("result", {}).get("result", {}).get("value", False)
                            if has_vid:
                                pages_with_video.add(page_target["url"])
                                break
                    except Exception:
                        continue
        return pages_with_video

    @staticmethod
    def _parse_stream_label(title: str, fallback_url: str = "") -> str:
        """Extract a human-readable stream label from a page title or URL.

        Streaming sites typically use titles like:
          "Watch Anaheim Ducks vs Buffalo Sabres Stream Delta 1 - Streamed"
        We extract: "Anaheim Ducks vs Buffalo Sabres"

        If the title is empty/unhelpful, parse the URL slug:
          "https://streamed.pk/watch/alabama-vs-texas-tech-2453656/admin/1"
        We extract: "alabama vs texas tech"
        """
        if title and " Stream " in title:
            label = title.split(" Stream ")[0]
            label = label.replace("Watch ", "").strip()
            if label:
                return label
        if title and len(title) > 3:
            for suffix in [" - Streamed", " | Streamed", " - OnHockey", " | OnHockey"]:
                if suffix in title:
                    title = title.split(suffix)[0].strip()
            return title[:60]
        # Fall back to URL path parsing
        if fallback_url and "/watch/" in fallback_url:
            import re
            # Extract slug after /watch/ e.g. "alabama-vs-texas-tech-2453656"
            slug = fallback_url.split("/watch/")[1].split("/")[0]
            # Strip trailing numeric ID
            slug = re.sub(r'-\d{4,}$', '', slug)
            # Convert dashes to spaces, title-case
            label = slug.replace("-", " ").strip().title()
            if label:
                return label
        if fallback_url:
            return fallback_url.split("/")[-1][:40]
        return "unknown"

    async def keepalive(self) -> str:
        """Scan all open browser tabs, attach health monitors to any with <video> elements.

        In CDP mode, reconnects first to pick up tabs opened after StreamKeeper started.
        Uses CDP /json/list as fallback to detect video in cross-origin iframes.
        """
        if not self.context:
            return "No browser context. Start the browser first."

        # Reconnect CDP to pick up newly opened tabs
        if self._cdp_url:
            await self._reconnect_cdp()

        if not self.context:
            return "CDP reconnect failed — no browser context."

        pages = self.context.pages
        attached = 0
        skipped = 0
        # After CDP reconnect, page objects are new — match by URL
        already_urls = set(s.url or "" for s in self.active_streams.values())

        # Pre-fetch CDP-based video detection for cross-origin iframes
        cdp_video_urls = await self._get_urls_with_video_cdp()

        for page in pages:
            page_url = page.url
            if page_url in already_urls or page_url in ("about:blank", "chrome://newtab/"):
                skipped += 1
                continue

            # Check if page has a video element — try frames first, fall back to CDP iframe scan
            try:
                has_video = False
                for frame in page.frames:
                    try:
                        has_video = await frame.evaluate("() => !!document.querySelector('video')")
                        if has_video:
                            break
                    except Exception:
                        continue
            except Exception:
                has_video = False

            if not has_video:
                has_video = page_url in cdp_video_urls

            # Even without detected video, attach to known streaming site pages
            # (the video may be in an unreachable cross-origin iframe)
            is_streaming_page = False
            if not has_video:
                streaming_domains = ["streamed.", "onhockey.", "embedsports.", "sportsurge."]
                is_streaming_page = any(d in page_url for d in streaming_domains)

            if not has_video and not is_streaming_page:
                continue

            if len(self.active_streams) >= self.max_streams:
                break

            # Extract a human-readable label from the page title
            try:
                title = await page.title()
            except Exception:
                title = ""
            label = self._parse_stream_label(title, page_url)
            stream_id = self._make_stream_id(team=label)

            video_note = "video detected" if has_video else "streaming page (video in cross-origin iframe)"
            logger.info(f"[keepalive] Attaching monitor to: {label} [{stream_id}] ({video_note})")

            health_mon = HealthMonitor(self.config)
            stream = ActiveStream(
                id=stream_id,
                team=label,
                page=page,
                driver=None,
                health_monitor=health_mon,
                site="keepalive",
                url=page_url,
            )
            stream.monitor_task = asyncio.create_task(self._monitor_loop(stream))
            self.active_streams[stream_id] = stream
            attached += 1

        if attached == 0:
            return (
                f"No new streams found ({len(pages)} tabs open, {skipped} already monitored).\n"
                "Open your streams in the browser first, then run keepalive again."
            )

        return (
            f"Keepalive active -- monitoring {attached} new stream{'s' if attached != 1 else ''}\n"
            f"Total monitored: {len(self.active_streams)}/{self.max_streams}\n"
            f"Health check every {self.config.get('health', {}).get('poll_interval_seconds', 5)}s | Auto-recovery enabled"
        )

    async def _watch_cdp_native(self, team: str, use_site: str, stream_id: str):
        """CDP-native watch: use API for game lookup, open tab via Target.createTarget.

        Opens the tab in Chrome's native context (with user extensions, no Playwright
        ad blocking), which is required for streamed.pk embeds to load properly.

        Returns (page, driver, game) on success, or (None, None, error_str) on failure.
        """
        import aiohttp as _aiohttp
        from sites import StreamedPKDriver, GameInfo

        # Step 1: Use API to find the game (no browser page needed)
        base_url = self.config.get("sites", {}).get("streamed.pk", {}).get("base_url", "https://streamed.pk")
        api_url = f"{base_url}/api/matches/hockey"
        team_lower = team.lower()
        game = None

        try:
            async with _aiohttp.ClientSession() as session:
                async with session.get(api_url, timeout=_aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        matches = await resp.json()
                        for match in matches:
                            teams_data = match.get("teams", {})
                            home = teams_data.get("home", {}).get("name", "")
                            away = teams_data.get("away", {}).get("name", "")
                            title = match.get("title", "")
                            match_id = match.get("id", "")
                            names = [home.lower(), away.lower(), title.lower()]
                            parts = team_lower.split()
                            found = any(team_lower in n for n in names)
                            if not found:
                                found = any(p in n for p in parts for n in names if len(p) > 3)
                            if found:
                                sources = match.get("sources", [])
                                game = {
                                    "title": title or f"{home} vs {away}",
                                    "match_id": match_id,
                                    "home": home,
                                    "away": away,
                                    "sources": sources,
                                }
                                break
        except Exception as e:
            logger.warning(f"API game lookup failed: {e}")

        if not game:
            return None, None, f"❌ No game found for **{team}** on {use_site}"

        logger.info(f"API found game: {game['title']} ({len(game['sources'])} sources)")

        # Step 2: Pick the best source (admin stream 1)
        source_path = "admin/1"  # Default: admin stream 1
        for src in game["sources"]:
            if src.get("source") == "admin":
                source_path = "admin/1"
                break

        # Step 3: Open tab via CDP Target.createTarget (native browser context)
        watch_url = f"{base_url}/watch/{game['match_id']}/{source_path}"
        cdp_base = self._get_cdp_base()

        try:
            async with _aiohttp.ClientSession() as sess:
                # Get any existing page's WS URL to send Target.createTarget
                async with sess.get(f"{cdp_base}/json/list") as r:
                    cdp_targets = await r.json()

                ws_url = None
                for t in cdp_targets:
                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                        ws_url = t["webSocketDebuggerUrl"]
                        break

                if not ws_url:
                    return None, None, "❌ No CDP page available to create new tab"

                async with sess.ws_connect(ws_url) as ws:
                    await ws.send_json({
                        "id": 1,
                        "method": "Target.createTarget",
                        "params": {"url": watch_url}
                    })
                    resp = await asyncio.wait_for(ws.receive_json(), timeout=10)
                    target_id = resp.get("result", {}).get("targetId")
                    logger.info(f"CDP opened native tab: {watch_url} (target: {target_id})")
        except Exception as e:
            logger.error(f"CDP Target.createTarget failed: {e}")
            return None, None, f"❌ Failed to open stream tab: {e}"

        # Step 4: Wait for page to load, then auto-setup (theater + play)
        await asyncio.sleep(8)  # Wait for React hydration + embed iframe load

        # Auto-play JW Player in the stream's pooembed iframe
        try:
            async with _aiohttp.ClientSession() as sess:
                async with sess.get(f"{cdp_base}/json/list") as r:
                    cdp_targets = await r.json()

                # Find the pooembed iframe for this game and click play
                for t in cdp_targets:
                    iframe_url = t.get("url", "")
                    if t.get("type") != "iframe" or "pooembed" not in iframe_url:
                        continue
                    home_parts = game["home"].lower().split()
                    away_parts = game["away"].lower().split()
                    if not any(p[:3] in iframe_url.lower() for p in home_parts + away_parts if len(p) >= 3):
                        continue

                    iframe_ws = t.get("webSocketDebuggerUrl")
                    if not iframe_ws:
                        continue
                    try:
                        async with sess.ws_connect(iframe_ws) as ws:
                            # Get video element center position for click
                            await ws.send_json({"id": 1, "method": "Runtime.evaluate", "params": {
                                "expression": "(function(){ var v = document.querySelector('video'); if (!v) return null; var r = v.getBoundingClientRect(); return {x: Math.round(r.x + r.width/2), y: Math.round(r.y + r.height/2), w: r.width}; })()",
                                "returnByValue": True
                            }})
                            resp = await asyncio.wait_for(ws.receive_json(), timeout=5)
                            pos = resp.get("result", {}).get("result", {}).get("value")

                            if pos and pos.get("w", 0) > 0:
                                # Simulate mouse click (satisfies autoplay policy)
                                x, y = pos["x"], pos["y"]
                                await ws.send_json({"id": 2, "method": "Input.dispatchMouseEvent", "params": {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1}})
                                await asyncio.wait_for(ws.receive_json(), timeout=3)
                                await ws.send_json({"id": 3, "method": "Input.dispatchMouseEvent", "params": {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1}})
                                await asyncio.wait_for(ws.receive_json(), timeout=3)
                                logger.info(f"CDP click-to-play at ({x},{y}) on {iframe_url[:60]}")

                                # Unmute via JS after click
                                await asyncio.sleep(1)
                                await ws.send_json({"id": 4, "method": "Runtime.evaluate", "params": {
                                    "expression": "(function(){ try { var api = jwplayer(); api.setMute(false); api.setVolume(100); return 'state=' + api.getState(); } catch(e) { var v = document.querySelector('video'); if(v){v.muted=false;v.volume=1;} return 'direct'; } })()",
                                    "returnByValue": True
                                }})
                                resp = await asyncio.wait_for(ws.receive_json(), timeout=5)
                                val = resp.get("result", {}).get("result", {}).get("value", "")
                                logger.info(f"CDP auto-play result: {val}")
                            else:
                                logger.warning(f"Video element not positioned on {iframe_url[:60]} — may need manual play")
                    except Exception as e:
                        logger.warning(f"CDP auto-play failed on {iframe_url[:60]}: {e}")
                    break

                # Enable theater mode on the watch page
                for t in cdp_targets:
                    target_url = t.get("url", "")
                    if t.get("type") != "page" or game["match_id"] not in target_url:
                        continue
                    page_ws = t.get("webSocketDebuggerUrl")
                    if not page_ws:
                        continue
                    try:
                        async with sess.ws_connect(page_ws) as ws:
                            js = """(function(){
                                var btns = document.querySelectorAll('button');
                                for (var i=0; i<btns.length; i++) {
                                    var t = (btns[i].innerText || '').trim().toLowerCase();
                                    if (t.includes('enter theater')) { btns[i].click(); return 'theater-on'; }
                                }
                                return 'no-theater';
                            })()"""
                            await ws.send_json({"id": 1, "method": "Runtime.evaluate", "params": {"expression": js, "returnByValue": True}})
                            resp = await asyncio.wait_for(ws.receive_json(), timeout=5)
                            val = resp.get("result", {}).get("result", {}).get("value", "")
                            logger.info(f"CDP theater mode: {val}")
                    except Exception:
                        pass
                    break
        except Exception as e:
            logger.warning(f"CDP auto-setup failed (non-fatal): {e}")

        # Step 5: Find the Playwright page object for this tab (after CDP reconnect)
        await self._reconnect_cdp()
        page = None
        if self.context:
            for p in self.context.pages:
                if game["match_id"] in (p.url or ""):
                    page = p
                    break

        if not page:
            # Create a dummy page reference — health monitoring will use CDP directly
            logger.warning("Could not find Playwright page for CDP-opened tab — using CDP-only monitoring")
            page = await self._new_stream_page()
            await page.goto(watch_url, wait_until="domcontentloaded", timeout=30000)

        driver = get_driver(page, self.config, use_site)
        driver.current_game = GameInfo(
            title=game["title"],
            teams=[game["home"], game["away"]],
            url=watch_url,
            is_live=True,
        )

        return page, driver, game
        """Start watching a game for the given team in a new tab."""
        if len(self.active_streams) >= self.max_streams:
            return f"❌ Max streams ({self.max_streams}) reached. Stop one first with `!stop <name>`."

        if not self.context:
            await self.start_browser()

        use_site = site or self.default_site
        stream_id = self._make_stream_id(team=team)

        logger.info(f"Starting watch [{stream_id}]: {team} on {use_site}")

        # --- CDP mode: use API for game lookup, open tab natively via CDP ---
        if self._cdp_url and use_site == "streamed.pk":
            page, driver, game = await self._watch_cdp_native(team, use_site, stream_id)
            if page is None:
                return game  # game holds the error string
        else:
            # --- Headless / non-CDP mode: use Playwright pages ---
            page = await self._new_stream_page()
            driver = get_driver(page, self.config, use_site)

            nav_ok = await driver.navigate_to_games()
            if not nav_ok:
                logger.warning(f"Page navigation to {use_site} failed — will try API-based game lookup")

            game = await driver.find_game(team)
            if not game:
                await page.close()
                return f"❌ No game found for **{team}** on {use_site}"

            if not await driver.open_game(game):
                await page.close()
                return f"❌ Failed to open game page for: {game.title}"

            await driver.list_sources()
            if not await driver.load_stream(0):
                pass

        # Create ActiveStream
        health_mon = HealthMonitor(self.config)
        health_mon.history.recovery_count = 0
        # Set stream URL for CDP health checks — use the page's current URL
        stream_url = page.url if page and not page.is_closed() else None
        stream = ActiveStream(
            id=stream_id,
            team=team,
            page=page,
            driver=driver,
            health_monitor=health_mon,
            site=use_site,
            url=stream_url,
        )
        stream.monitor_task = asyncio.create_task(self._monitor_loop(stream))
        self.active_streams[stream_id] = stream

        sources_count = len(driver.available_sources)
        game_title = game["title"] if isinstance(game, dict) else game.title
        return (
            f"🏒 Now watching: **{game_title}** [`{stream_id}`]\n"
            f"📺 Site: {use_site}\n"
            f"🔗 Sources available: {sources_count}\n"
            f"🛡️ Ad blocking active | Health monitoring started\n"
            f"📊 Active streams: {len(self.active_streams)}/{self.max_streams}"
        )

    # ------------------------------------------------------------------
    # stop — per-stream or all
    # ------------------------------------------------------------------

    async def stop(self, stream_id: Optional[str] = None) -> str:
        """Stop a specific stream, or all streams if stream_id is None."""
        if stream_id and stream_id.lower() == "all":
            stream_id = None  # Treat "all" as stop-everything

        if stream_id:
            # Find stream by ID (case-insensitive) or partial match
            stream = self._find_stream(stream_id)
            if not stream:
                return f"❌ No active stream matching `{stream_id}`"
            return await self._stop_stream(stream)

        # Stop all streams
        if not self.active_streams:
            return "😴 Nothing is playing"

        names = [s.team for s in self.active_streams.values()]
        # Copy keys to avoid mutation during iteration
        for sid in list(self.active_streams.keys()):
            stream = self.active_streams.get(sid)
            if stream:
                await self._stop_stream(stream)

        return f"⏹️ Stopped all streams: {', '.join(names)}"

    async def _stop_stream(self, stream: ActiveStream) -> str:
        """Stop and clean up a single stream."""
        stream_id = stream.id
        team = stream.team

        # Cancel monitor task
        if stream.monitor_task:
            stream.monitor_task.cancel()
            try:
                await stream.monitor_task
            except asyncio.CancelledError:
                pass

        # Close the page/tab
        try:
            if not stream.page.is_closed():
                await stream.page.close()
        except Exception as e:
            logger.warning(f"Error closing page for {stream_id}: {e}")

        # Remove from active streams
        self.active_streams.pop(stream_id, None)

        logger.info(f"Stopped stream [{stream_id}]: {team}")
        return f"⏹️ Stopped watching {team} [`{stream_id}`] — {len(self.active_streams)} stream(s) remaining"

    def _find_stream(self, query: str) -> Optional[ActiveStream]:
        """Find stream by exact ID, or case-insensitive partial match on ID or team name."""
        q = query.lower().strip()

        # Exact ID match
        if q in self.active_streams:
            return self.active_streams[q]

        # Partial match on ID or team name
        for sid, stream in self.active_streams.items():
            if q in sid or q in stream.team.lower():
                return stream

        return None

    # ------------------------------------------------------------------
    # shutdown
    # ------------------------------------------------------------------

    async def shutdown(self):
        """Full shutdown — stop all streams, close browser and Playwright."""
        await self.stop()  # stops all
        if self._cdp_url:
            # CDP mode — disconnect without closing the external Chrome
            if self._browser:
                try:
                    await self._browser.close()
                except Exception:
                    pass
                self._browser = None
            self.context = None
        else:
            if self.context:
                await self.context.close()
                self.context = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        logger.info("StreamKeeper shut down")

    # ------------------------------------------------------------------
    # get_status — per-stream or all
    # ------------------------------------------------------------------

    async def get_status(self, stream_id: Optional[str] = None) -> str:
        """Get status for a specific stream or all streams."""
        if not self.active_streams:
            return "😴 Not currently watching anything"

        if stream_id:
            stream = self._find_stream(stream_id)
            if not stream:
                return f"❌ No active stream matching `{stream_id}`"
            return self._format_stream_status(stream)

        # All streams summary
        ads = self.ad_handler.stats
        lines = [f"📊 **{len(self.active_streams)} active stream(s)** (max {self.max_streams})\n"]

        for stream in self.active_streams.values():
            lines.append(self._format_stream_status(stream, compact=True))

        lines.append(f"\n🛡️ **Ads blocked**: {ads['requests_blocked']} | Overlays dismissed: {ads['overlays_dismissed']}")
        return "\n".join(lines)

    def _format_stream_status(self, stream: ActiveStream, compact: bool = False) -> str:
        """Format status for a single stream."""
        health = stream.health_monitor.stats
        uptime = int(time.time() - stream.started_at)
        uptime_str = f"{uptime // 3600}h{(uptime % 3600) // 60}m" if uptime >= 3600 else f"{uptime // 60}m{uptime % 60}s"

        if compact:
            state_icon = "🟢" if health["state"] == "playing" else "🟡" if health["state"] in ("loading", "stalled") else "🔴"
            return (
                f"{state_icon} **{stream.team}** [`{stream.id}`] — "
                f"{health['state']} | {health['resolution']} | "
                f"up {uptime_str} | recoveries: {health['recovery_count']}"
            )

        source = stream.site if stream.site != "direct" else (stream.url[:50] + "..." if stream.url else "direct URL")
        lines = [
            f"🏒 **Watching**: {stream.team} [`{stream.id}`] on {source}",
            f"📊 **Stream**: {health['state']} | {health['resolution']}",
            f"⏱️ **Position**: {health['current_time']} | Buffered to: {health['buffered_to']}",
            f"🔊 **Audio**: {health['audio']} (muted={health['muted']}, vol={health['volume']})",
            f"🔄 **Recoveries**: {health['recovery_count']} | Stall: {health['stall_duration']}",
            f"⏳ **Uptime**: {uptime_str}",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Per-stream actions
    # ------------------------------------------------------------------

    async def take_screenshot(self, stream_id: Optional[str] = None) -> Optional[str | list[str]]:
        """Take a screenshot. If stream_id given, screenshot that stream. Otherwise all."""
        os.makedirs("./screenshots", exist_ok=True)

        if stream_id:
            stream = self._find_stream(stream_id)
            if not stream:
                return None
            return await self._screenshot_stream(stream)

        if not self.active_streams:
            return None

        # Screenshot all streams
        paths = []
        for stream in self.active_streams.values():
            path = await self._screenshot_stream(stream)
            if path:
                paths.append(path)
        return paths if paths else None

    async def _screenshot_stream(self, stream: ActiveStream) -> Optional[str]:
        """Take a screenshot of a single stream's page."""
        try:
            if stream.page.is_closed():
                return None
            path = f"./screenshots/{stream.id}-{int(time.time())}.png"
            await stream.page.screenshot(path=path)
            logger.info(f"Screenshot saved: {path}")
            return path
        except Exception as e:
            logger.warning(f"Screenshot failed for {stream.id}: {e}")
            return None

    async def force_unmute(self, stream_id: Optional[str] = None) -> str:
        """Force unmute a stream (first stream if no ID given)."""
        stream = self._resolve_stream(stream_id)
        if not stream:
            return "❌ No active stream" if not stream_id else f"❌ No stream matching `{stream_id}`"
        success = await stream.health_monitor.force_unmute(stream.page)
        return f"🔊 Unmuted {stream.team}" if success else f"❌ Failed to unmute {stream.team} (no video element found)"

    async def force_reload(self, stream_id: Optional[str] = None) -> str:
        """Force reload a stream."""
        stream = self._resolve_stream(stream_id)
        if not stream:
            return "❌ No active stream" if not stream_id else f"❌ No stream matching `{stream_id}`"
        if not stream.driver:
            # Direct URL watch — reload the page
            try:
                await stream.page.reload(wait_until="domcontentloaded", timeout=30000)
                return f"🔄 Reloaded {stream.team} (page refresh)"
            except Exception as e:
                return f"❌ Reload failed for {stream.team}: {e}"
        success = await stream.driver.reload_stream()
        return f"🔄 Reloaded {stream.team}" if success else f"❌ Reload failed for {stream.team}"

    async def switch_source(self, stream_id: Optional[str] = None) -> str:
        """Switch to the next stream source for a stream."""
        stream = self._resolve_stream(stream_id)
        if not stream:
            return "❌ No active stream" if not stream_id else f"❌ No stream matching `{stream_id}`"
        if not stream.driver:
            return f"❌ {stream.team} is a direct URL watch — no sources to switch"
        success = await stream.driver.next_source()
        if success:
            idx = stream.driver.current_source_index
            name = stream.driver.available_sources[idx].name if idx < len(stream.driver.available_sources) else "?"
            return f"🔀 Switched {stream.team} to source: {name}"
        return f"❌ No more sources available for {stream.team}"

    def _resolve_stream(self, stream_id: Optional[str] = None) -> Optional[ActiveStream]:
        """Resolve a stream by ID, or return the first active stream if None."""
        if stream_id:
            return self._find_stream(stream_id)
        if self.active_streams:
            return next(iter(self.active_streams.values()))
        return None

    # ------------------------------------------------------------------
    # Health monitoring loop (per-stream)
    # ------------------------------------------------------------------

    async def _monitor_loop(self, stream: ActiveStream):
        """Health monitoring loop for a single stream — runs until stopped."""
        logger.info(f"Health monitoring loop started for [{stream.id}]")
        poll_interval = stream.health_monitor.poll_interval
        consecutive_no_video = 0

        while stream.id in self.active_streams:
            try:
                await asyncio.sleep(poll_interval)
                if stream.id not in self.active_streams:
                    break
                if stream.page.is_closed():
                    logger.warning(f"Page closed for [{stream.id}], removing stream")
                    self.active_streams.pop(stream.id, None)
                    break

                # Take health snapshot
                snap = await stream.health_monitor.check_health(stream.page)

                # If video element is unreachable (cross-origin iframe), use CDP
                if snap.state == StreamState.NO_VIDEO:
                    consecutive_no_video += 1
                    if consecutive_no_video >= 2 and stream.url and self.config.get("browser", {}).get("remote_debugging_url"):
                        # Try CDP to reach the video through cross-origin iframes
                        cdp_health = await self._check_video_health_cdp(stream.url)
                        if cdp_health:
                            # Map CDP result to a HealthSnapshot
                            snap.ready_state = cdp_health.get("readyState", 0)
                            snap.current_time = cdp_health.get("currentTime", 0)
                            snap.paused = cdp_health.get("paused", False)
                            snap.ended = cdp_health.get("ended", False)
                            snap.muted = cdp_health.get("muted", False)
                            snap.volume = cdp_health.get("volume", 1.0)
                            snap.video_width = cdp_health.get("videoWidth", 0)
                            snap.video_height = cdp_health.get("videoHeight", 0)
                            snap.buffered_end = cdp_health.get("bufferedEnd", 0)
                            err = cdp_health.get("error")
                            if err:
                                snap.error_code = err.get("code")
                                snap.error_message = err.get("message", "")
                            snap.state = stream.health_monitor._determine_state(snap)
                            stream.health_monitor.history.add(snap)
                            if snap.state == StreamState.PLAYING:
                                consecutive_no_video = 0
                                logger.info(f"[{stream.id}] CDP health: PLAYING {snap.video_width}x{snap.video_height} @ {snap.current_time:.1f}s")
                            elif snap.state in (StreamState.FROZEN, StreamState.STALLED, StreamState.ERROR):
                                logger.warning(f"[{stream.id}] CDP health: {snap.state.value}")
                            continue
                        else:
                            # CDP found no video either — page-level fallback
                            snap = await stream.health_monitor.check_health_page_level(stream.page)
                            if snap.is_healthy:
                                continue
                else:
                    consecutive_no_video = 0

                # Periodically dismiss any new ad overlays
                try:
                    await self.ad_handler.dismiss_overlays(stream.page)
                except Exception:
                    pass

                # Check if recovery is needed
                if stream.health_monitor.needs_recovery() and not stream.recovery_in_progress:
                    reason = stream.health_monitor.get_recovery_reason()
                    logger.warning(f"Stream [{stream.id}] unhealthy: {reason}")
                    await self._attempt_recovery(stream, reason)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Monitor loop error [{stream.id}]: {e}", exc_info=True)
                await asyncio.sleep(5)

        logger.info(f"Health monitoring loop stopped for [{stream.id}]")

    async def _attempt_recovery(self, stream: ActiveStream, reason: str):
        """Graduated recovery cascade for a single stream."""
        if stream.recovery_in_progress:
            return
        if stream.health_monitor.history.seconds_since_recovery < self.recovery_cooldown:
            logger.debug(f"Recovery on cooldown for [{stream.id}], skipping")
            return
        if stream.health_monitor.history.recovery_count >= self.max_recovery_attempts:
            logger.error(
                f"Max recovery attempts ({self.max_recovery_attempts}) reached for [{stream.id}]. "
                "Manual intervention needed."
            )
            if self._discord_notify:
                await self._discord_notify(
                    f"🚨 **StreamKeeper needs help!**\n"
                    f"Max recovery attempts reached for {stream.team} [`{stream.id}`].\n"
                    f"Reason: {reason}\n"
                    f"Use `!reload {stream.team}` or `!switch {stream.team}` to try manually."
                )
            return

        stream.recovery_in_progress = True
        stream.health_monitor.history.recovery_count += 1
        stream.health_monitor.history.last_recovery = time.time()
        attempt = stream.health_monitor.history.recovery_count

        logger.info(f"Recovery attempt #{attempt} for [{stream.id}]: {reason}")

        if self.screenshot_on_failure:
            await self._screenshot_stream(stream)

        try:
            # Level 0: CDP force play (for cross-origin iframe streams)
            if attempt <= 2 and stream.url and self.config.get("browser", {}).get("remote_debugging_url"):
                logger.info(f"Recovery L0 [{stream.id}]: CDP force play through iframe")
                cdp_ok = await self._force_play_cdp(stream.url)
                if cdp_ok:
                    await asyncio.sleep(3)
                    cdp_health = await self._check_video_health_cdp(stream.url)
                    if cdp_health and cdp_health.get("readyState", 0) >= 3 and cdp_health.get("currentTime", 0) > 0:
                        logger.info(f"Recovery L0 succeeded for [{stream.id}] via CDP!")
                        if self._discord_notify:
                            await self._discord_notify(
                                f"🔧 Auto-recovered (CDP play) — {stream.team} stream back"
                            )
                        return

            # Level 1: Soft fix — play + unmute (direct page, non-iframe)
            if attempt <= 2:
                logger.info(f"Recovery L1 [{stream.id}]: Force play + unmute")
                await stream.health_monitor.force_play(stream.page)
                await asyncio.sleep(3)

                snap = await stream.health_monitor.check_health(stream.page)
                if snap.is_healthy:
                    logger.info(f"Recovery L1 succeeded for [{stream.id}]!")
                    if self._discord_notify:
                        await self._discord_notify(
                            f"🔧 Auto-recovered (play+unmute) — {stream.team} stream back"
                        )
                    return

            # Level 2: Reload the stream iframe / re-click source
            if attempt <= 3:
                logger.info(f"Recovery L2 [{stream.id}]: Reload stream")
                if stream.driver:
                    await stream.driver.reload_stream()
                elif stream.url:
                    await stream.page.reload(wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(5)

                snap = await stream.health_monitor.check_health(stream.page)
                if snap.is_healthy:
                    logger.info(f"Recovery L2 succeeded for [{stream.id}]!")
                    if self._discord_notify:
                        await self._discord_notify(
                            f"🔄 Auto-recovered (reload) — {stream.team} stream back"
                        )
                    return

            # Level 3: Try next mirror
            if attempt <= 4:
                logger.info(f"Recovery L3 [{stream.id}]: Switch to next source")
                if stream.driver:
                    success = await stream.driver.next_source()
                    if success:
                        await asyncio.sleep(5)
                        snap = await stream.health_monitor.check_health(stream.page)
                        if snap.is_healthy:
                            logger.info(f"Recovery L3 succeeded for [{stream.id}]!")
                            if self._discord_notify:
                                await self._discord_notify(
                                    f"🔀 Auto-recovered (mirror switch) — {stream.team} stream back"
                                )
                            return

            # Level 4: Full restart from scratch
            logger.info(f"Recovery L4 [{stream.id}]: Full restart")
            if stream.driver and stream.team:
                await stream.driver.navigate_to_games()
                game = await stream.driver.find_game(stream.team)
                if game:
                    await stream.driver.open_game(game)
                    await stream.driver.list_sources()
                    await stream.driver.load_stream(0)
                    await asyncio.sleep(5)

                    snap = await stream.health_monitor.check_health(stream.page)
                    if snap.is_healthy:
                        logger.info(f"Recovery L4 succeeded for [{stream.id}]!")
                        if self._discord_notify:
                            await self._discord_notify(
                                f"🔁 Auto-recovered (full restart) — {stream.team} stream back"
                            )
                        return

            # If we get here, all recovery failed
            logger.error(f"All recovery levels failed for [{stream.id}]")
            if self._discord_notify:
                await self._discord_notify(
                    f"⚠️ Recovery attempt #{attempt} failed for {stream.team} [`{stream.id}`].\n"
                    f"Reason: {reason}"
                )

        except Exception as e:
            logger.error(f"Recovery error [{stream.id}]: {e}", exc_info=True)
        finally:
            stream.recovery_in_progress = False

    # Callback for Discord notifications (set by the bot)
    _discord_notify = None


# ---------------------------------------------------------------------------
# Discord Bot
# ---------------------------------------------------------------------------

class StreamKeeperBot:
    """Discord bot interface for StreamKeeper."""

    def __init__(self, config: dict, keeper: StreamKeeper):
        self.config = config
        self.keeper = keeper

        discord_cfg = config.get("discord", {})
        prefix = discord_cfg.get("command_prefix", "!")

        intents = discord.Intents.default()
        intents.message_content = True

        self.bot = commands.Bot(command_prefix=prefix, intents=intents)
        self.notification_channel_id = discord_cfg.get("notification_channel_id")

        # Wire up the notification callback
        keeper._discord_notify = self._send_notification

        self._register_commands()

    def _register_commands(self):
        bot = self.bot
        keeper = self.keeper

        @bot.event
        async def on_ready():
            logger.info(f"Discord bot connected as {bot.user}")
            if self.notification_channel_id:
                ch = bot.get_channel(self.notification_channel_id)
                if ch:
                    await ch.send("🟢 StreamKeeper is online and ready!")

        @bot.command(name="keepalive")
        async def cmd_keepalive(ctx):
            """Monitor all open tabs with video streams. Open your streams first, then run this."""
            result = await keeper.keepalive()
            await ctx.send(result)

        @bot.command(name="watch_url")
        async def cmd_watch_url(ctx, *, url: str = ""):
            """Watch a direct stream URL in a new tab. Usage: !watch_url https://..."""
            if not url:
                await ctx.send("Usage: `!watch_url <url>` — provide a direct stream URL")
                return
            label = url.split("/watch/")[-1].split("/")[0] if "/watch/" in url else None
            await ctx.send(f"🔍 Opening stream in new tab...")
            result = await keeper.watch_url(url, label)
            await ctx.send(result)

        @bot.command(name="watch")
        async def cmd_watch(ctx, *, args: str = ""):
            """Watch a game in a new tab. Usage: !watch Blues [--site onhockey]"""
            parts = args.split("--site")
            team = parts[0].strip()
            site = parts[1].strip() if len(parts) > 1 else None

            if not team:
                favs = keeper.config.get("defaults", {}).get("favorite_teams", [])
                if favs:
                    await ctx.send(
                        f"Usage: `!watch <team>` — your favorites: {', '.join(favs)}"
                    )
                else:
                    await ctx.send("Usage: `!watch <team>` e.g. `!watch Blues`")
                return

            await ctx.send(f"🔍 Looking for {team} game on {site or keeper.default_site}...")
            result = await keeper.watch(team, site)
            await ctx.send(result)

        @bot.command(name="status")
        async def cmd_status(ctx, *, args: str = ""):
            """Show stream status. Usage: !status [stream_name]"""
            stream_id = args.strip() if args.strip() else None
            status = await keeper.get_status(stream_id)
            await ctx.send(status)

        @bot.command(name="stop")
        async def cmd_stop(ctx, *, args: str = ""):
            """Stop a stream. Usage: !stop Blues | !stop all"""
            target = args.strip() if args.strip() else None
            result = await keeper.stop(target)
            await ctx.send(result)

        @bot.command(name="reload")
        async def cmd_reload(ctx, *, args: str = ""):
            """Force reload a stream. Usage: !reload [stream_name]"""
            stream_id = args.strip() if args.strip() else None
            result = await keeper.force_reload(stream_id)
            await ctx.send(result)

        @bot.command(name="unmute")
        async def cmd_unmute(ctx, *, args: str = ""):
            """Force unmute a stream. Usage: !unmute [stream_name]"""
            stream_id = args.strip() if args.strip() else None
            result = await keeper.force_unmute(stream_id)
            await ctx.send(result)

        @bot.command(name="switch")
        async def cmd_switch(ctx, *, args: str = ""):
            """Switch to next stream mirror. Usage: !switch [stream_name]"""
            stream_id = args.strip() if args.strip() else None
            result = await keeper.switch_source(stream_id)
            await ctx.send(result)

        @bot.command(name="screenshot")
        async def cmd_screenshot(ctx, *, args: str = ""):
            """Screenshot a stream. Usage: !screenshot [stream_name]"""
            stream_id = args.strip() if args.strip() else None
            result = await keeper.take_screenshot(stream_id)

            if result is None:
                await ctx.send("❌ No active stream to screenshot")
            elif isinstance(result, list):
                for path in result:
                    await ctx.send(file=discord.File(path))
            else:
                await ctx.send(file=discord.File(result))

        @bot.command(name="sk_help")
        async def cmd_help_sk(ctx):
            """Show StreamKeeper commands."""
            help_text = (
                "**StreamKeeper Commands (multi-stream):**\n"
                "`!watch <team>` — Find and watch a game (new tab)\n"
                "`!watch <team> --site onhockey` — Use specific site\n"
                "`!watch_url <url>` — Watch direct URL (new tab)\n"
                "`!status` — Show all active streams\n"
                "`!status <name>` — Show specific stream detail\n"
                "`!reload <name>` — Force reload stream\n"
                "`!unmute <name>` — Force unmute audio\n"
                "`!switch <name>` — Try next stream mirror\n"
                "`!screenshot [name]` — Screenshot stream(s)\n"
                "`!stop <name>` — Stop specific stream\n"
                "`!stop all` — Stop everything\n"
            )
            await ctx.send(help_text)

    async def _send_notification(self, message: str):
        """Send a notification to the configured Discord channel."""
        if not self.notification_channel_id:
            return
        channel = self.bot.get_channel(self.notification_channel_id)
        if channel:
            try:
                await channel.send(message)
            except Exception as e:
                logger.error(f"Failed to send Discord notification: {e}")

    async def run(self):
        """Start the Discord bot."""
        token = self.config.get("discord", {}).get("bot_token", "")
        if not token or token == "YOUR_DISCORD_BOT_TOKEN":
            logger.error("No Discord bot token configured!")
            logger.info("Set your token in config.yaml or run in CLI mode")
            return
        await self.bot.start(token)


# ---------------------------------------------------------------------------
# CLI mode (no Discord)
# ---------------------------------------------------------------------------

async def run_cli(config: dict):
    """Run StreamKeeper in CLI mode (no Discord)."""
    keeper = StreamKeeper(config)

    # Notification callback for CLI — just log it
    async def cli_notify(msg: str):
        print(f"\n📢 {msg}\n")
    keeper._discord_notify = cli_notify

    print("🏒 StreamKeeper — CLI Mode (multi-stream)")
    print("=" * 40)

    team = input("Team to watch (e.g. Blues): ").strip()
    if not team:
        favs = config.get("defaults", {}).get("favorite_teams", [])
        if favs:
            team = favs[0]
            print(f"Using default: {team}")
        else:
            print("No team specified, exiting.")
            return

    site = input(f"Site [{config.get('defaults', {}).get('site', 'streamed.pk')}]: ").strip()
    if not site:
        site = None

    result = await keeper.watch(team, site)
    print(f"\n{result}\n")

    if keeper.is_watching:
        print("Stream is running. Press Ctrl+C to stop.")
        print("Commands: status, reload, unmute, switch, screenshot, stop, watch <team>, stop <name>, stop all")
        try:
            while keeper.is_watching:
                try:
                    cmd = await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(None, input),
                        timeout=None
                    )
                    parts = cmd.strip().split(maxsplit=1)
                    action = parts[0].lower() if parts else ""
                    arg = parts[1].strip() if len(parts) > 1 else None

                    if action == "status":
                        print(await keeper.get_status(arg))
                    elif action == "reload":
                        print(await keeper.force_reload(arg))
                    elif action == "unmute":
                        print(await keeper.force_unmute(arg))
                    elif action == "switch":
                        print(await keeper.switch_source(arg))
                    elif action == "screenshot":
                        result = await keeper.take_screenshot(arg)
                        if isinstance(result, list):
                            for p in result:
                                print(f"Saved: {p}")
                        elif result:
                            print(f"Saved: {result}")
                        else:
                            print("Failed")
                    elif action == "watch" and arg:
                        site_parts = arg.split("--site")
                        t = site_parts[0].strip()
                        s = site_parts[1].strip() if len(site_parts) > 1 else None
                        print(await keeper.watch(t, s))
                    elif action == "watch_url" and arg:
                        print(await keeper.watch_url(arg))
                    elif action == "stop":
                        print(await keeper.stop(arg))
                        if not keeper.is_watching:
                            break
                    else:
                        print("Unknown command. Try: status, watch <team>, stop <name>, stop all, reload, unmute, switch, screenshot")
                except EOFError:
                    break
        except KeyboardInterrupt:
            print("\n⏹️ Stopping...")

    await keeper.shutdown()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def main():
    # Load config
    config_path = "config.yaml"
    if not os.path.exists(config_path):
        # Try example config
        if os.path.exists("config.example.yaml"):
            print("⚠️  No config.yaml found — copy config.example.yaml to config.yaml and configure it")
            sys.exit(1)
        else:
            print("❌ No config file found")
            sys.exit(1)

    with open(config_path) as f:
        config = yaml.safe_load(f)

    setup_logging(config)

    # Determine mode
    mode = "discord" if HAS_DISCORD else "cli"
    token = config.get("discord", {}).get("bot_token", "")
    if token == "YOUR_DISCORD_BOT_TOKEN" or not token:
        mode = "cli"

    if mode == "discord":
        keeper = StreamKeeper(config)
        bot = StreamKeeperBot(config, keeper)
        api = StreamKeeperAPI(keeper, config)

        # Start browser before bot connects
        await keeper.start_browser()

        # Start API server alongside the Discord bot
        await api.start()

        # Run bot (blocking)
        try:
            await bot.run()
        except KeyboardInterrupt:
            pass
        finally:
            await api.stop()
            await keeper.shutdown()
    elif config.get("api", {}).get("enabled"):
        # API-only mode — no Discord, no CLI, just the HTTP API
        keeper = StreamKeeper(config)
        api = StreamKeeperAPI(keeper, config)

        await keeper.start_browser()
        await api.start()

        logger.info("StreamKeeper running in API-only mode (no Discord, no CLI)")
        logger.info(f"API server: http://0.0.0.0:{config.get('api', {}).get('port', 8890)}")

        # Keep running until interrupted
        try:
            while True:
                await asyncio.sleep(60)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await api.stop()
            await keeper.shutdown()
    else:
        # CLI mode
        keeper = StreamKeeper(config)
        api = StreamKeeperAPI(keeper, config)
        await api.start()
        await run_cli(config)
        await api.stop()


if __name__ == "__main__":
    asyncio.run(main())
