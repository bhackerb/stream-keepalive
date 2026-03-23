"""
StreamKeeper — Main orchestrator.

Ties together:
- Playwright browser (headed, with uBlock Origin)
- Site drivers (streamed.pk, onhockey.tv)
- Health monitoring loop
- Recovery cascade
- Discord bot interface
"""

import asyncio
import logging
import os
import sys
import time
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
# StreamKeeper core
# ---------------------------------------------------------------------------

class StreamKeeper:
    """Core orchestrator for stream monitoring and recovery."""

    def __init__(self, config: dict):
        self.config = config
        self.ad_handler = AdHandler(config)
        self.health_monitor = HealthMonitor(config)
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.driver: Optional[BaseSiteDriver] = None
        self.is_watching = False
        self.current_team: Optional[str] = None
        self.current_site: str = config.get("defaults", {}).get("site", "streamed.pk")
        self._monitor_task: Optional[asyncio.Task] = None
        self._playwright = None
        self._recovery_in_progress = False

        # Recovery settings
        health_cfg = config.get("health", {})
        self.max_recovery_attempts = health_cfg.get("max_recovery_attempts", 5)
        self.recovery_cooldown = health_cfg.get("recovery_cooldown_seconds", 30)
        self.screenshot_on_failure = health_cfg.get("screenshot_on_failure", True)

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

        # Get the first page or create one
        if self.context.pages:
            self.page = self.context.pages[0]
        else:
            self.page = await self.context.new_page()

        logger.info("Browser started successfully")

    async def _on_new_page(self, page: Page):
        """Handle new pages (popup ad windows)."""
        await asyncio.sleep(1)  # Let it load briefly
        handled = await self.ad_handler.handle_new_page_popup(page)
        if not handled:
            logger.info(f"New page opened: {page.url[:80]}")

    async def watch(self, team: str, site: Optional[str] = None) -> str:
        """Start watching a game for the given team."""
        if self.is_watching:
            await self.stop()

        self.current_team = team
        if site:
            self.current_site = site

        logger.info(f"Starting watch: {team} on {self.current_site}")

        if not self.context:
            await self.start_browser()

        # Create site driver
        self.driver = get_driver(self.page, self.config, self.current_site)

        # Navigate to games
        if not await self.driver.navigate_to_games():
            return f"❌ Failed to load {self.current_site} game listings"

        # Find the game
        game = await self.driver.find_game(team)
        if not game:
            return f"❌ No game found for **{team}** on {self.current_site}"

        # Open the game page
        if not await self.driver.open_game(game):
            return f"❌ Failed to open game page for: {game.title}"

        # List and load stream sources
        await self.driver.list_sources()
        if not await self.driver.load_stream(0):
            return (
                f"⚠️ Opened game page for **{game.title}** but couldn't find video player. "
                "The stream might need manual interaction to start."
            )

        # Start health monitoring
        self.is_watching = True
        self.health_monitor.history.recovery_count = 0
        self._monitor_task = asyncio.create_task(self._monitor_loop())

        sources_count = len(self.driver.available_sources)
        return (
            f"🏒 Now watching: **{game.title}**\n"
            f"📺 Site: {self.current_site}\n"
            f"🔗 Sources available: {sources_count}\n"
            f"🛡️ Ad blocking active | Health monitoring started"
        )

    async def stop(self) -> str:
        """Stop watching and clean up."""
        self.is_watching = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None

        team = self.current_team or "stream"
        self.current_team = None
        self.driver = None
        logger.info("Stopped watching")
        return f"⏹️ Stopped watching {team}"

    async def shutdown(self):
        """Full shutdown — close browser and Playwright."""
        await self.stop()
        if self.context:
            await self.context.close()
            self.context = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        logger.info("StreamKeeper shut down")

    async def get_status(self) -> str:
        """Get current status as a formatted string."""
        if not self.is_watching:
            return "😴 Not currently watching anything"

        health = self.health_monitor.stats
        ads = self.ad_handler.stats

        lines = [
            f"🏒 **Watching**: {self.current_team} on {self.current_site}",
            f"📊 **Stream**: {health['state']} | {health['resolution']}",
            f"⏱️ **Position**: {health['current_time']} | Buffered to: {health['buffered_to']}",
            f"🔊 **Audio**: {health['audio']} (muted={health['muted']}, vol={health['volume']})",
            f"🔄 **Recoveries**: {health['recovery_count']} | Stall: {health['stall_duration']}",
            f"🛡️ **Ads blocked**: {ads['requests_blocked']} | Overlays dismissed: {ads['overlays_dismissed']}",
        ]
        return "\n".join(lines)

    async def take_screenshot(self) -> Optional[str]:
        """Take a screenshot of the current page."""
        if not self.page:
            return None
        path = f"./screenshots/stream-{int(time.time())}.png"
        os.makedirs("./screenshots", exist_ok=True)
        await self.page.screenshot(path=path)
        logger.info(f"Screenshot saved: {path}")
        return path

    async def force_unmute(self) -> str:
        """Force unmute the stream."""
        if not self.page:
            return "❌ No active page"
        success = await self.health_monitor.force_unmute(self.page)
        return "🔊 Unmuted" if success else "❌ Failed to unmute (no video element found)"

    async def force_reload(self) -> str:
        """Force reload the current stream."""
        if not self.driver:
            return "❌ No active stream"
        success = await self.driver.reload_stream()
        return "🔄 Reloaded" if success else "❌ Reload failed"

    async def switch_source(self) -> str:
        """Switch to the next stream source."""
        if not self.driver:
            return "❌ No active stream"
        success = await self.driver.next_source()
        if success:
            idx = self.driver.current_source_index
            name = self.driver.available_sources[idx].name if idx < len(self.driver.available_sources) else "?"
            return f"🔀 Switched to source: {name}"
        return "❌ No more sources available"

    # ------------------------------------------------------------------
    # Health monitoring loop
    # ------------------------------------------------------------------

    async def _monitor_loop(self):
        """Main health monitoring loop — runs until stopped."""
        logger.info("Health monitoring loop started")
        poll_interval = self.health_monitor.poll_interval

        while self.is_watching:
            try:
                await asyncio.sleep(poll_interval)
                if not self.is_watching:
                    break

                # Take health snapshot
                snap = await self.health_monitor.check_health(self.page)

                # Periodically dismiss any new ad overlays
                await self.ad_handler.dismiss_overlays(self.page)

                # Check if recovery is needed
                if self.health_monitor.needs_recovery() and not self._recovery_in_progress:
                    reason = self.health_monitor.get_recovery_reason()
                    logger.warning(f"Stream unhealthy: {reason}")
                    await self._attempt_recovery(reason)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Monitor loop error: {e}", exc_info=True)
                await asyncio.sleep(5)

        logger.info("Health monitoring loop stopped")

    async def _attempt_recovery(self, reason: str):
        """Graduated recovery cascade."""
        if self._recovery_in_progress:
            return
        if self.health_monitor.history.seconds_since_recovery < self.recovery_cooldown:
            logger.debug("Recovery on cooldown, skipping")
            return
        if self.health_monitor.history.recovery_count >= self.max_recovery_attempts:
            logger.error(
                f"Max recovery attempts ({self.max_recovery_attempts}) reached. "
                "Manual intervention needed."
            )
            if self._discord_notify:
                await self._discord_notify(
                    f"🚨 **StreamKeeper needs help!**\n"
                    f"Max recovery attempts reached for {self.current_team}.\n"
                    f"Reason: {reason}\n"
                    f"Use `!reload` or `!switch` to try manually."
                )
            return

        self._recovery_in_progress = True
        self.health_monitor.history.recovery_count += 1
        self.health_monitor.history.last_recovery = time.time()
        attempt = self.health_monitor.history.recovery_count

        logger.info(f"Recovery attempt #{attempt}: {reason}")

        if self.screenshot_on_failure:
            await self.take_screenshot()

        try:
            # Level 1: Soft fix — play + unmute
            if attempt <= 2:
                logger.info("Recovery L1: Force play + unmute")
                await self.health_monitor.force_play(self.page)
                await asyncio.sleep(3)

                snap = await self.health_monitor.check_health(self.page)
                if snap.is_healthy:
                    logger.info("Recovery L1 succeeded!")
                    if self._discord_notify:
                        await self._discord_notify(
                            f"🔧 Auto-recovered (play+unmute) — {self.current_team} stream back"
                        )
                    return

            # Level 2: Reload the stream iframe / re-click source
            if attempt <= 3:
                logger.info("Recovery L2: Reload stream")
                if self.driver:
                    await self.driver.reload_stream()
                    await asyncio.sleep(5)

                    snap = await self.health_monitor.check_health(self.page)
                    if snap.is_healthy:
                        logger.info("Recovery L2 succeeded!")
                        if self._discord_notify:
                            await self._discord_notify(
                                f"🔄 Auto-recovered (reload) — {self.current_team} stream back"
                            )
                        return

            # Level 3: Try next mirror
            if attempt <= 4:
                logger.info("Recovery L3: Switch to next source")
                if self.driver:
                    success = await self.driver.next_source()
                    if success:
                        await asyncio.sleep(5)
                        snap = await self.health_monitor.check_health(self.page)
                        if snap.is_healthy:
                            logger.info("Recovery L3 succeeded!")
                            if self._discord_notify:
                                await self._discord_notify(
                                    f"🔀 Auto-recovered (mirror switch) — {self.current_team} stream back"
                                )
                            return

            # Level 4: Full restart from scratch
            logger.info("Recovery L4: Full restart")
            if self.driver and self.current_team:
                await self.driver.navigate_to_games()
                game = await self.driver.find_game(self.current_team)
                if game:
                    await self.driver.open_game(game)
                    await self.driver.list_sources()
                    await self.driver.load_stream(0)
                    await asyncio.sleep(5)

                    snap = await self.health_monitor.check_health(self.page)
                    if snap.is_healthy:
                        logger.info("Recovery L4 succeeded!")
                        if self._discord_notify:
                            await self._discord_notify(
                                f"🔁 Auto-recovered (full restart) — {self.current_team} stream back"
                            )
                        return

            # If we get here, all recovery failed
            logger.error("All recovery levels failed")
            if self._discord_notify:
                await self._discord_notify(
                    f"⚠️ Recovery attempt #{attempt} failed for {self.current_team}.\n"
                    f"Reason: {reason}"
                )

        except Exception as e:
            logger.error(f"Recovery error: {e}", exc_info=True)
        finally:
            self._recovery_in_progress = False

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

        @bot.command(name="watch")
        async def cmd_watch(ctx, *, args: str = ""):
            """Start watching a game. Usage: !watch Blues [--site onhockey]"""
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

            await ctx.send(f"🔍 Looking for {team} game on {site or keeper.current_site}...")
            result = await keeper.watch(team, site)
            await ctx.send(result)

        @bot.command(name="status")
        async def cmd_status(ctx):
            """Show current stream status."""
            status = await keeper.get_status()
            await ctx.send(status)

        @bot.command(name="stop")
        async def cmd_stop(ctx):
            """Stop watching."""
            result = await keeper.stop()
            await ctx.send(result)

        @bot.command(name="reload")
        async def cmd_reload(ctx):
            """Force reload the stream."""
            result = await keeper.force_reload()
            await ctx.send(result)

        @bot.command(name="unmute")
        async def cmd_unmute(ctx):
            """Force unmute the stream."""
            result = await keeper.force_unmute()
            await ctx.send(result)

        @bot.command(name="switch")
        async def cmd_switch(ctx):
            """Switch to next stream mirror."""
            result = await keeper.switch_source()
            await ctx.send(result)

        @bot.command(name="screenshot")
        async def cmd_screenshot(ctx):
            """Take a screenshot of the current stream."""
            path = await keeper.take_screenshot()
            if path:
                await ctx.send(file=discord.File(path))
            else:
                await ctx.send("❌ No active stream to screenshot")

        @bot.command(name="sk_help")
        async def cmd_help_sk(ctx):
            """Show StreamKeeper commands."""
            help_text = (
                "**StreamKeeper Commands:**\n"
                "`!watch <team>` — Find and watch a game\n"
                "`!watch <team> --site onhockey` — Use specific site\n"
                "`!status` — Show stream health\n"
                "`!reload` — Force reload stream\n"
                "`!unmute` — Force unmute audio\n"
                "`!switch` — Try next stream mirror\n"
                "`!screenshot` — Screenshot current state\n"
                "`!stop` — Stop watching\n"
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

    print("🏒 StreamKeeper — CLI Mode")
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
        print("Commands: status, reload, unmute, switch, screenshot, stop")
        try:
            while keeper.is_watching:
                # Simple command loop
                try:
                    cmd = await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(None, input),
                        timeout=None
                    )
                    cmd = cmd.strip().lower()
                    if cmd == "status":
                        print(await keeper.get_status())
                    elif cmd == "reload":
                        print(await keeper.force_reload())
                    elif cmd == "unmute":
                        print(await keeper.force_unmute())
                    elif cmd == "switch":
                        print(await keeper.switch_source())
                    elif cmd == "screenshot":
                        path = await keeper.take_screenshot()
                        print(f"Saved: {path}" if path else "Failed")
                    elif cmd == "stop":
                        print(await keeper.stop())
                        break
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
