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

from playwright.async_api import async_playwright, BrowserContext, Page

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

        # Launch persistent context (headed mode for watching + extensions)
        self.context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir,
            headless=False,
            channel="chromium",
            args=args,
            viewport={"width": viewport_w, "height": viewport_h},
            ignore_default_args=["--disable-extensions"],
            # Accept all permissions
            permissions=["geolocation"],
        )

        # Set up ad blocking on the context
        await self.ad_handler.setup_network_blocking(self.context)

        # Handle popup windows (ad popups)
        self.context.on("page", self._on_new_page)

        logger.info("Browser started successfully")

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

        # Wait for video element to appear
        await asyncio.sleep(3)

        # Dismiss any initial ad overlays
        try:
            await self.ad_handler.dismiss_overlays(page)
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

    async def watch(self, team: str, site: Optional[str] = None) -> str:
        """Start watching a game for the given team in a new tab."""
        if len(self.active_streams) >= self.max_streams:
            return f"❌ Max streams ({self.max_streams}) reached. Stop one first with `!stop <name>`."

        if not self.context:
            await self.start_browser()

        use_site = site or self.default_site
        stream_id = self._make_stream_id(team=team)

        logger.info(f"Starting watch [{stream_id}]: {team} on {use_site}")

        page = await self._new_stream_page()

        # Create site driver for this page
        driver = get_driver(page, self.config, use_site)

        # Navigate to games
        if not await driver.navigate_to_games():
            await page.close()
            return f"❌ Failed to load {use_site} game listings"

        # Find the game
        game = await driver.find_game(team)
        if not game:
            await page.close()
            return f"❌ No game found for **{team}** on {use_site}"

        # Open the game page
        if not await driver.open_game(game):
            await page.close()
            return f"❌ Failed to open game page for: {game.title}"

        # List and load stream sources
        await driver.list_sources()
        if not await driver.load_stream(0):
            # Keep the page open — might need manual interaction
            pass

        # Create ActiveStream
        health_mon = HealthMonitor(self.config)
        health_mon.history.recovery_count = 0
        stream = ActiveStream(
            id=stream_id,
            team=team,
            page=page,
            driver=driver,
            health_monitor=health_mon,
            site=use_site,
        )
        stream.monitor_task = asyncio.create_task(self._monitor_loop(stream))
        self.active_streams[stream_id] = stream

        sources_count = len(driver.available_sources)
        return (
            f"🏒 Now watching: **{game.title}** [`{stream_id}`]\n"
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
                await stream.health_monitor.check_health(stream.page)

                # Periodically dismiss any new ad overlays
                await self.ad_handler.dismiss_overlays(stream.page)

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
            # Level 1: Soft fix — play + unmute
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
    else:
        # CLI mode — still start the API server if enabled
        keeper = StreamKeeper(config)
        api = StreamKeeperAPI(keeper, config)
        await api.start()
        await run_cli(config)
        await api.stop()


if __name__ == "__main__":
    asyncio.run(main())
