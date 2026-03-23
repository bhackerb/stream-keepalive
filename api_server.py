"""HTTP API for StreamKeeper — allows remote control from Mithrandir or other devices."""

import asyncio
import json
import logging
import os
import time
from typing import Optional, TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from stream_keeper import StreamKeeper

logger = logging.getLogger("stream-keeper.api")


def _bearer_token(request: web.Request) -> Optional[str]:
    """Extract bearer token from Authorization header."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return None


def _check_auth(request: web.Request, api_key: str) -> bool:
    """Validate bearer token against configured API key."""
    token = _bearer_token(request)
    return token == api_key


class StreamKeeperAPI:
    """HTTP API server for remote StreamKeeper control."""

    def __init__(self, keeper: "StreamKeeper", config: dict):
        self.keeper = keeper
        self.config = config
        self.api_config = config.get("api", {})
        self.port = self.api_config.get("port", 8890)
        self.api_key = self.api_config.get("api_key", "")
        self.app = web.Application(middlewares=[self._auth_middleware])
        self._setup_routes()
        self._runner: Optional[web.AppRunner] = None
        self._start_time = time.time()

    def _setup_routes(self):
        self.app.router.add_get("/health", self.handle_health)
        self.app.router.add_get("/status", self.handle_status)
        self.app.router.add_post("/watch", self.handle_watch)
        self.app.router.add_post("/stop", self.handle_stop)
        self.app.router.add_post("/reload", self.handle_reload)
        self.app.router.add_post("/unmute", self.handle_unmute)
        self.app.router.add_post("/switch", self.handle_switch)
        self.app.router.add_get("/screenshot", self.handle_screenshot)

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        # Health endpoint is unauthenticated
        if request.path == "/health":
            return await handler(request)

        # All other endpoints require auth
        if not self.api_key or self.api_key == "YOUR_API_KEY":
            # No API key configured — allow all (dev mode)
            logger.warning("API key not configured — allowing unauthenticated access")
            return await handler(request)

        if not _check_auth(request, self.api_key):
            return web.json_response(
                {"error": "unauthorized", "message": "Invalid or missing Bearer token"},
                status=401,
            )

        return await handler(request)

    async def handle_health(self, _request: web.Request) -> web.Response:
        """Simple health check — is the service running?"""
        return web.json_response({
            "status": "ok",
            "uptime_seconds": round(time.time() - self._start_time),
            "is_watching": self.keeper.is_watching,
        })

    async def handle_status(self, _request: web.Request) -> web.Response:
        """Current stream status as JSON."""
        if not self.keeper.is_watching:
            return web.json_response({
                "is_watching": False,
                "team": None,
                "site": None,
                "health": None,
            })

        health = self.keeper.health_monitor.stats
        ads = self.keeper.ad_handler.stats

        return web.json_response({
            "is_watching": True,
            "team": self.keeper.current_team,
            "site": self.keeper.current_site,
            "health": {
                "state": health["state"],
                "resolution": health["resolution"],
                "current_time": health["current_time"],
                "buffered_to": health["buffered_to"],
                "audio": health["audio"],
                "muted": health["muted"],
                "volume": health["volume"],
                "stall_duration": health["stall_duration"],
                "recovery_count": health["recovery_count"],
            },
            "ads": {
                "requests_blocked": ads["requests_blocked"],
                "overlays_dismissed": ads["overlays_dismissed"],
            },
        })

    async def handle_watch(self, request: web.Request) -> web.Response:
        """Start watching a game. Body: {"team": "Blues", "site": "streamed.pk"}"""
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"error": "invalid_body", "message": "Expected JSON body with 'team' field"},
                status=400,
            )

        team = body.get("team", "").strip()
        if not team:
            return web.json_response(
                {"error": "missing_team", "message": "'team' field is required"},
                status=400,
            )

        # Check if this is a direct URL watch
        url = body.get("url", "").strip()
        if url:
            label = body.get("label", "").strip() or None
            logger.info(f"API: watch_url request — url={url[:60]}, label={label}")
            result = await self.keeper.watch_url(url, label)
            success = not result.startswith("\u274c")
            return web.json_response({
                "success": success,
                "message": result,
                "url": url,
            })

        site = body.get("site")
        logger.info(f"API: watch request — team={team}, site={site}")

        result = await self.keeper.watch(team, site)
        success = not result.startswith("\u274c")  # doesn't start with red X emoji

        return web.json_response({
            "success": success,
            "message": result,
            "team": team,
            "site": site or self.keeper.current_site,
        })

    async def handle_stop(self, _request: web.Request) -> web.Response:
        """Stop watching."""
        result = await self.keeper.stop()
        return web.json_response({"success": True, "message": result})

    async def handle_reload(self, _request: web.Request) -> web.Response:
        """Force reload the stream."""
        result = await self.keeper.force_reload()
        return web.json_response({
            "success": not result.startswith("\u274c"),
            "message": result,
        })

    async def handle_unmute(self, _request: web.Request) -> web.Response:
        """Force unmute the stream."""
        result = await self.keeper.force_unmute()
        return web.json_response({
            "success": not result.startswith("\u274c"),
            "message": result,
        })

    async def handle_switch(self, _request: web.Request) -> web.Response:
        """Switch to next stream source."""
        result = await self.keeper.switch_source()
        return web.json_response({
            "success": not result.startswith("\u274c"),
            "message": result,
        })

    async def handle_screenshot(self, _request: web.Request) -> web.Response:
        """Take a screenshot and return as image."""
        path = await self.keeper.take_screenshot()
        if not path:
            return web.json_response(
                {"error": "no_stream", "message": "No active page to screenshot"},
                status=404,
            )

        return web.FileResponse(path, headers={
            "Content-Type": "image/png",
            "Content-Disposition": f'inline; filename="{os.path.basename(path)}"',
        })

    async def start(self):
        """Start the API server."""
        if not self.api_config.get("enabled", False):
            logger.info("API server disabled in config")
            return

        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        await site.start()
        logger.info(f"API server started on port {self.port}")

    async def stop(self):
        """Stop the API server."""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            logger.info("API server stopped")
