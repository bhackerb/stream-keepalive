"""
Site Drivers — Per-site navigation logic for finding and loading streams.

Each site driver knows how to:
1. Navigate to the game listing
2. Find a specific game by team name
3. Click through ads/interstitials to reach the stream
4. Locate the video player element
5. List available mirror/source options
"""

import asyncio
import logging
import re
from abc import ABC, abstractmethod
from typing import Optional
from dataclasses import dataclass

from playwright.async_api import Page

logger = logging.getLogger("stream-keeper.sites")


@dataclass
class GameInfo:
    """Info about a found game listing."""
    title: str
    teams: list[str]
    url: Optional[str] = None
    time: Optional[str] = None
    is_live: bool = False


@dataclass
class StreamSource:
    """A single stream mirror/source option."""
    name: str
    url: Optional[str] = None
    element_index: int = 0  # Index in the list of source links


class BaseSiteDriver(ABC):
    """Abstract base class for site-specific navigation."""

    def __init__(self, page: Page, config: dict, site_key: str):
        self.page = page
        self.config = config
        self.site_config = config.get("sites", {}).get(site_key, {})
        self.site_key = site_key
        self.current_game: Optional[GameInfo] = None
        self.current_source_index: int = 0
        self.available_sources: list[StreamSource] = []

    @abstractmethod
    async def navigate_to_games(self) -> bool:
        """Navigate to the game listing page."""
        ...

    @abstractmethod
    async def find_game(self, team: str) -> Optional[GameInfo]:
        """Find a game matching the team name."""
        ...

    @abstractmethod
    async def open_game(self, game: GameInfo) -> bool:
        """Navigate to the game's stream page."""
        ...

    @abstractmethod
    async def load_stream(self, source_index: int = 0) -> bool:
        """Load a specific stream source. Returns True if video element found."""
        ...

    @abstractmethod
    async def list_sources(self) -> list[StreamSource]:
        """List available stream mirrors/sources."""
        ...

    async def next_source(self) -> bool:
        """Try the next available stream source."""
        self.current_source_index += 1
        if self.current_source_index >= len(self.available_sources):
            logger.warning("No more stream sources available")
            return False
        logger.info(
            f"Switching to source #{self.current_source_index}: "
            f"{self.available_sources[self.current_source_index].name}"
        )
        return await self.load_stream(self.current_source_index)

    async def reload_stream(self) -> bool:
        """Reload the current stream (page refresh + re-navigate)."""
        logger.info("Reloading current stream...")
        await self.page.reload(wait_until="domcontentloaded")
        await asyncio.sleep(2)
        return await self.load_stream(self.current_source_index)

    async def _wait_and_click(self, selector: str, timeout: int = 5000) -> bool:
        """Wait for an element and click it. Returns False if not found."""
        try:
            await self.page.wait_for_selector(selector, timeout=timeout)
            await self.page.click(selector)
            return True
        except Exception:
            return False

    async def _find_by_text(self, text: str, tag: str = "*") -> Optional[any]:
        """Find an element containing the given text (case-insensitive)."""
        try:
            elements = await self.page.query_selector_all(tag)
            text_lower = text.lower()
            for el in elements:
                el_text = await el.inner_text()
                if text_lower in el_text.lower():
                    return el
            return None
        except Exception:
            return None

    async def _dismiss_initial_ads(self, max_attempts: int = 3):
        """Dismiss initial ad overlays/popups that appear on page load."""
        for _ in range(max_attempts):
            await asyncio.sleep(1)
            # Try clicking common close buttons
            close_selectors = [
                "button.close", ".close-btn", "[class*='close']",
                "button[aria-label='Close']", ".modal-close",
                "#close-button", ".popup-close",
            ]
            for sel in close_selectors:
                try:
                    btn = await self.page.query_selector(sel)
                    if btn and await btn.is_visible():
                        await btn.click()
                        logger.debug(f"Clicked close button: {sel}")
                        await asyncio.sleep(0.5)
                except Exception:
                    pass

    async def _has_video_element(self) -> bool:
        """Check if a video element exists on the page or in iframes."""
        return await self.page.evaluate("""() => {
            if (document.querySelector('video')) return true;
            const iframes = document.querySelectorAll('iframe');
            for (const iframe of iframes) {
                try {
                    const doc = iframe.contentDocument || iframe.contentWindow?.document;
                    if (doc && doc.querySelector('video')) return true;
                } catch (e) {}
            }
            return false;
        }""")


class StreamedPKDriver(BaseSiteDriver):
    """Site driver for streamed.pk"""

    def __init__(self, page: Page, config: dict):
        super().__init__(page, config, "streamed.pk")
        self.base_url = self.site_config.get("base_url", "https://streamed.pk")

    async def navigate_to_games(self) -> bool:
        """Navigate to the hockey game listings."""
        path = self.site_config.get("game_list_path", "/category/ice-hockey")
        url = f"{self.base_url}{path}"
        logger.info(f"Navigating to game listings: {url}")
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)
            await self._dismiss_initial_ads()
            return True
        except Exception as e:
            logger.error(f"Failed to navigate to game listings: {e}")
            return False

    async def find_game(self, team: str) -> Optional[GameInfo]:
        """Find a game by team name on the listings page."""
        team_lower = team.lower()
        logger.info(f"Searching for game with team: {team}")

        # Strategy 1: Look for links/cards containing the team name
        games_found = await self.page.evaluate("""(teamName) => {
            const results = [];
            // Look for any clickable element containing the team name
            const allLinks = document.querySelectorAll('a');
            for (const link of allLinks) {
                const text = link.innerText || link.textContent || '';
                if (text.toLowerCase().includes(teamName.toLowerCase()) && text.length < 200) {
                    results.push({
                        text: text.trim().substring(0, 150),
                        href: link.href,
                        isLive: text.toLowerCase().includes('live') ||
                                link.closest('[class*="live"]') !== null
                    });
                }
            }
            return results;
        }""", team)

        if not games_found:
            # Strategy 2: Broader text search
            element = await self._find_by_text(team, "a")
            if element:
                text = await element.inner_text()
                href = await element.get_attribute("href")
                games_found = [{"text": text.strip()[:150], "href": href, "isLive": False}]

        if not games_found:
            logger.warning(f"No games found for team: {team}")
            return None

        # Pick the first (most relevant) result
        game_data = games_found[0]
        game = GameInfo(
            title=game_data["text"],
            teams=[team],  # We know at least this team is playing
            url=game_data.get("href"),
            is_live=game_data.get("isLive", False),
        )
        logger.info(f"Found game: {game.title}")
        self.current_game = game
        return game

    async def open_game(self, game: GameInfo) -> bool:
        """Navigate to the game's stream page."""
        if not game.url:
            logger.error("Game has no URL")
            return False

        logger.info(f"Opening game page: {game.url}")
        try:
            # Handle relative URLs
            url = game.url if game.url.startswith("http") else f"{self.base_url}{game.url}"
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)
            await self._dismiss_initial_ads()
            return True
        except Exception as e:
            logger.error(f"Failed to open game page: {e}")
            return False

    async def list_sources(self) -> list[StreamSource]:
        """List available stream sources on the game page."""
        sources = await self.page.evaluate("""() => {
            const results = [];
            // Look for stream source links/buttons
            const candidates = document.querySelectorAll(
                'a[href*="stream"], a[href*="source"], a[href*="link"],' +
                'button[class*="source"], [class*="stream-link"],' +
                'a[class*="btn"], .source-item a, .stream-option a'
            );
            let idx = 0;
            for (const el of candidates) {
                const text = (el.innerText || el.textContent || '').trim();
                if (text && text.length < 100) {
                    results.push({
                        name: text.substring(0, 50),
                        href: el.href || null,
                        index: idx++
                    });
                }
            }
            return results;
        }""")

        self.available_sources = [
            StreamSource(
                name=s["name"],
                url=s.get("href"),
                element_index=s["index"]
            )
            for s in sources
        ]
        logger.info(f"Found {len(self.available_sources)} stream sources")
        return self.available_sources

    async def load_stream(self, source_index: int = 0) -> bool:
        """Click a stream source and wait for the video player to appear."""
        self.current_source_index = source_index

        # If we have sources, click the right one
        if self.available_sources and source_index < len(self.available_sources):
            source = self.available_sources[source_index]
            logger.info(f"Loading stream source: {source.name}")

            # Click the source link
            try:
                source_links = await self.page.query_selector_all(
                    'a[href*="stream"], a[href*="source"], .source-item a, .stream-option a'
                )
                if source_index < len(source_links):
                    await source_links[source_index].click()
                    await asyncio.sleep(3)
            except Exception as e:
                logger.warning(f"Failed to click source link: {e}")

        # Dismiss any ads that appeared
        await self._dismiss_initial_ads()

        # Wait for video element
        for attempt in range(10):
            if await self._has_video_element():
                logger.info("Video element found!")
                # Try to auto-play and unmute
                await self.page.evaluate("""() => {
                    const videos = document.querySelectorAll('video');
                    for (const v of videos) {
                        v.muted = false;
                        v.volume = 1.0;
                        v.play().catch(() => {});
                    }
                    // Also check iframes
                    for (const iframe of document.querySelectorAll('iframe')) {
                        try {
                            const doc = iframe.contentDocument || iframe.contentWindow?.document;
                            if (doc) {
                                const v = doc.querySelector('video');
                                if (v) {
                                    v.muted = false;
                                    v.volume = 1.0;
                                    v.play().catch(() => {});
                                }
                            }
                        } catch(e) {}
                    }
                }""")
                return True
            await asyncio.sleep(2)

        logger.warning("Timed out waiting for video element")
        return False


class OnHockeyTVDriver(BaseSiteDriver):
    """Site driver for onhockey.tv"""

    def __init__(self, page: Page, config: dict):
        super().__init__(page, config, "onhockey.tv")
        self.base_url = self.site_config.get("base_url", "https://www.onhockey.tv")

    async def navigate_to_games(self) -> bool:
        """Navigate to the main page (game listings)."""
        logger.info(f"Navigating to: {self.base_url}")
        try:
            await self.page.goto(self.base_url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)
            await self._dismiss_initial_ads()
            return True
        except Exception as e:
            logger.error(f"Failed to navigate: {e}")
            return False

    async def find_game(self, team: str) -> Optional[GameInfo]:
        """Find a game by team name."""
        team_lower = team.lower()
        logger.info(f"Searching for team: {team}")

        # OnHockey typically uses table rows for game listings
        games_found = await self.page.evaluate("""(teamName) => {
            const results = [];
            // Search table rows and links
            const rows = document.querySelectorAll('tr, .game-row, [class*="game"]');
            for (const row of rows) {
                const text = row.innerText || '';
                if (text.toLowerCase().includes(teamName.toLowerCase())) {
                    const link = row.querySelector('a');
                    results.push({
                        text: text.trim().replace(/\\s+/g, ' ').substring(0, 150),
                        href: link ? link.href : null
                    });
                }
            }
            // Also check plain links
            if (results.length === 0) {
                const links = document.querySelectorAll('a');
                for (const link of links) {
                    const text = link.innerText || '';
                    if (text.toLowerCase().includes(teamName.toLowerCase()) && text.length < 200) {
                        results.push({
                            text: text.trim().substring(0, 150),
                            href: link.href
                        });
                    }
                }
            }
            return results;
        }""", team)

        if not games_found:
            logger.warning(f"No games found for: {team}")
            return None

        game_data = games_found[0]
        game = GameInfo(
            title=game_data["text"],
            teams=[team],
            url=game_data.get("href"),
        )
        logger.info(f"Found game: {game.title}")
        self.current_game = game
        return game

    async def open_game(self, game: GameInfo) -> bool:
        """Navigate to the game stream page."""
        if not game.url:
            # Try clicking the element directly
            el = await self._find_by_text(game.teams[0], "a")
            if el:
                await el.click()
                await asyncio.sleep(2)
                await self._dismiss_initial_ads()
                return True
            return False

        try:
            url = game.url if game.url.startswith("http") else f"{self.base_url}{game.url}"
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2)
            await self._dismiss_initial_ads()
            return True
        except Exception as e:
            logger.error(f"Failed to open game: {e}")
            return False

    async def list_sources(self) -> list[StreamSource]:
        """List available stream sources."""
        sources = await self.page.evaluate("""() => {
            const results = [];
            const links = document.querySelectorAll('a[href*="np_"], a[href*="stream"]');
            let idx = 0;
            for (const link of links) {
                const text = (link.innerText || link.textContent || '').trim();
                if (text) {
                    results.push({ name: text.substring(0, 50), href: link.href, index: idx++ });
                }
            }
            return results;
        }""")

        self.available_sources = [
            StreamSource(name=s["name"], url=s.get("href"), element_index=s["index"])
            for s in sources
        ]
        logger.info(f"Found {len(self.available_sources)} stream sources")
        return self.available_sources

    async def load_stream(self, source_index: int = 0) -> bool:
        """Load a stream source and wait for video."""
        self.current_source_index = source_index

        if self.available_sources and source_index < len(self.available_sources):
            source = self.available_sources[source_index]
            if source.url:
                try:
                    await self.page.goto(source.url, wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(3)
                except Exception as e:
                    logger.warning(f"Failed to load source URL: {e}")

        await self._dismiss_initial_ads()

        for attempt in range(10):
            if await self._has_video_element():
                logger.info("Video element found!")
                await self.page.evaluate("""() => {
                    const videos = document.querySelectorAll('video');
                    for (const v of videos) {
                        v.muted = false;
                        v.volume = 1.0;
                        v.play().catch(() => {});
                    }
                }""")
                return True
            await asyncio.sleep(2)

        logger.warning("Timed out waiting for video element")
        return False


def get_driver(page: Page, config: dict, site: str) -> BaseSiteDriver:
    """Factory function to get the right driver for a site."""
    drivers = {
        "streamed.pk": StreamedPKDriver,
        "streamed": StreamedPKDriver,
        "onhockey.tv": OnHockeyTVDriver,
        "onhockey": OnHockeyTVDriver,
    }
    driver_cls = drivers.get(site.lower())
    if not driver_cls:
        raise ValueError(f"Unknown site: {site}. Available: {list(drivers.keys())}")
    return driver_cls(page, config)
