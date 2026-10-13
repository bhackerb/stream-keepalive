"""
Ad Handler — Network-level ad blocking and overlay dismissal.

Two layers:
1. Network interception via Playwright route() — blocks requests before they load
2. DOM overlay detection — finds and dismisses ad overlays that slip through
"""

import logging
import re
from urllib.parse import urlsplit
from typing import Optional

from playwright.async_api import Page, Route, BrowserContext

logger = logging.getLogger("stream-keeper.ads")

# Default blocked patterns (supplemented by config)
DEFAULT_BLOCKED_PATTERNS = [
    r"googleads", r"googlesyndication", r"doubleclick\.net",
    r"adservice\.google", r"pagead2\.googlesyndication",
    r"adnxs\.com", r"adsrvr\.org", r"adform\.net",
    r"taboola\.com", r"outbrain\.com",
    r"popads\.net", r"popcash\.net", r"propellerads",
    r"exoclick\.com", r"juicyads\.com", r"trafficjunky",
    r"hilltopads", r"clickadu", r"pushground",
    r"ad-maven", r"admaven",
    r"wonderblockoffer", r"wildcasino",
    r"(?:^|\.)wildfreedome\.com$", r"(?:^|\.)budgetezy\.org$",
    r"bet365", r"stake\.com", r"1xbet", r"betway",
    r"bongacams", r"chaturbate", r"livejasmin",
    r"popunder", r"clickaine", r"revcontent",
    r"mgid\.com", r"zergnet\.com",
]

# Resource types that are almost always ads or tracking
BLOCKED_RESOURCE_TYPES = {"beacon"}
CANARY_URL = "https://doubleclick.net.streamkeeper.invalid/adblock-canary"


class AdHandler:
    """Handles ad blocking at network and DOM levels."""

    def __init__(self, config: dict):
        self.config = config
        self.blocked_count = 0
        self.overlays_dismissed = 0
        self.network_enabled = False
        self.network_verified = False

        # Build compiled regex from config + defaults
        extra_patterns = config.get("ads", {}).get("blocked_domain_patterns", [])
        all_patterns = DEFAULT_BLOCKED_PATTERNS + [
            re.escape(p).replace(r"\*", ".*") for p in extra_patterns
        ]
        self.block_regex = re.compile("|".join(all_patterns), re.IGNORECASE)

        extra_resource_types = config.get("ads", {}).get("blocked_resource_types", [])
        self.blocked_resource_types = BLOCKED_RESOURCE_TYPES | set(extra_resource_types)

    async def setup_network_blocking(self, context: BrowserContext):
        """Install network-level ad blocking on a browser context."""
        await context.route("**/*", self._route_handler)
        self.network_enabled = True
        logger.info("Network-level ad blocking installed on context")

    async def verify_network_blocking(self, context):
        """A reserved .invalid hostname probes the installed route, never a live ad service."""
        page = await context.new_page()
        try:
            await page.goto(CANARY_URL, timeout=5000)
        except Exception:
            pass  # An aborted canary is expected; only the route handler proves it.
        finally:
            await page.close()
        return self.network_verified

    async def setup_page_blocking(self, page: Page):
        """Install page-level ad blocking (for pages created outside initial context)."""
        await page.route("**/*", self._route_handler)
        logger.info(f"Network-level ad blocking installed on page: {page.url}")

    async def _route_handler(self, route: Route):
        """Intercept and block ad/tracking requests."""
        request = route.request
        url = request.url
        resource_type = request.resource_type

        # Block by resource type
        if resource_type in self.blocked_resource_types:
            self.blocked_count += 1
            logger.debug(f"Blocked [{resource_type}]: {url[:80]}")
            await route.abort()
            return

        # Block by URL pattern
        if self.block_regex.search(urlsplit(url).hostname or ""):
            self.blocked_count += 1
            if url == CANARY_URL:
                self.network_verified = True
            logger.debug(f"Blocked [pattern]: {url[:80]}")
            await route.abort()
            return

        # Allow everything else
        await route.continue_()

    async def dismiss_overlays(self, page: Page) -> int:
        """Find and dismiss ad overlays on the page. Returns count dismissed."""
        dismissed = 0

        site_config = self._get_site_config(page.url)
        overlay_selector = site_config.get("selectors", {}).get(
            "ad_overlay",
            ".ad-overlay, .ad-popup, .advertisement"
        )
        close_selector = site_config.get("selectors", {}).get(
            "close_button",
            ".close, .close-btn, [class*='close'], button[aria-label='Close']"
        )

        # Strategy 1: Click close buttons on overlays
        try:
            close_buttons = await page.query_selector_all(
                ', '.join(f'{overlay.strip()} {close.strip()}' for overlay in overlay_selector.split(',')
                          for close in close_selector.split(','))
            )
            for btn in close_buttons:
                if await btn.is_visible():
                    try:
                        await btn.click(timeout=2000)
                        dismissed += 1
                        logger.info("Dismissed overlay via close button")
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"Close button scan error: {e}")

        # Strategy 2: Remove overlay elements via JS
        removed = await page.evaluate("""() => {
            let removed = 0;
            // Remove fixed/absolute positioned overlays covering the viewport
            const all = document.querySelectorAll('div, section, aside');
            for (const el of all) {
                if (el.hasAttribute('data-sk-tab') || el.querySelector('[data-sk-tab]')) continue;
                const style = window.getComputedStyle(el);
                const isOverlay = (
                    (style.position === 'fixed' || style.position === 'absolute') &&
                    parseFloat(style.zIndex) > 999 &&
                    el.offsetWidth > window.innerWidth * 0.5 &&
                    el.offsetHeight > window.innerHeight * 0.3
                );
                // Check if it looks like an ad (not the video player)
                const isAd = (
                    !el.querySelector('video, iframe') &&
                    !el.closest('[class*="player"]') &&
                    (el.className + el.id).match(/ad|overlay|popup|modal|banner|promo/i)
                );
                if (isOverlay && isAd) {
                    el.remove();
                    removed++;
                }
            }
            // Embedded players often contain /admin/ in their URL. Never infer
            // advertising from a substring or dimensions; network filters own it.
            return removed;
        }""")

        dismissed += removed
        if removed > 0:
            logger.info(f"Removed {removed} overlay elements via JS")

        # Strategy 3: Handle new tab/window popups (close non-main pages)
        # This is handled at the context level in the orchestrator

        self.overlays_dismissed += dismissed
        return dismissed

    async def handle_new_page_popup(self, page: Page):
        """Close pages that look like ad popups."""
        url = page.url
        if self.block_regex.search(urlsplit(url).hostname or ""):
            logger.info(f"Closing popup page: {url[:60]}")
            await page.close()
            return True
        return False

    def _get_site_config(self, url: str) -> dict:
        """Get site-specific config based on current URL."""
        sites = self.config.get("sites", {})
        for site_key, site_conf in sites.items():
            if site_key.replace(".", "") in url.replace(".", ""):
                return site_conf
        return {}

    @property
    def stats(self) -> dict:
        return {
            "requests_blocked": self.blocked_count,
            "overlays_dismissed": self.overlays_dismissed,
            "network_enabled": self.network_enabled,
            "network_verified": self.network_verified,
            "extension_verified": False,
            "note": "Network routing installed" if self.network_enabled else "Attached browser: in-profile adblocker has not been verified",
        }
