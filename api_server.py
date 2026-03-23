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
        self.app.router.add_post("/keepalive", self.handle_keepalive)
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
            "active_streams": len(self.keeper.active_streams),
            "max_streams": self.keeper.max_streams,
        })

    async def handle_status(self, request: web.Request) -> web.Response:
        """Current stream status as JSON. Optional ?stream_id= for specific stream."""
        stream_id = request.query.get("stream_id")

        if not self.keeper.active_streams:
            return web.json_response({
                "is_watching": False,
                "streams": [],
            })

        ads = self.keeper.ad_handler.stats

        if stream_id:
            stream = self.keeper._find_stream(stream_id)
            if not stream:
                return web.json_response(
                    {"error": "not_found", "message": f"No stream matching '{stream_id}'"},
                    status=404,
                )
            return web.json_response({
                "is_watching": True,
                "stream": self._stream_to_dict(stream),
                "ads": {
                    "requests_blocked": ads["requests_blocked"],
                    "overlays_dismissed": ads["overlays_dismissed"],
                },
            })

        # All streams
        streams = [self._stream_to_dict(s) for s in self.keeper.active_streams.values()]
        return web.json_response({
            "is_watching": True,
            "active_count": len(streams),
            "max_streams": self.keeper.max_streams,
            "streams": streams,
            "ads": {
                "requests_blocked": ads["requests_blocked"],
                "overlays_dismissed": ads["overlays_dismissed"],
            },
        })

    def _stream_to_dict(self, stream) -> dict:
        """Convert an ActiveStream to a JSON-serializable dict."""
        health = stream.health_monitor.stats
        return {
            "stream_id": stream.id,
            "team": stream.team,
            "site": stream.site,
            "url": stream.url,
            "started_at": stream.started_at,
            "uptime_seconds": round(time.time() - stream.started_at),
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
        }

    async def handle_keepalive(self, _request: web.Request) -> web.Response:
        """Scan all open tabs and attach health monitors to any with video elements."""
        logger.info("API: keepalive request")
        result = await self.keeper.keepalive()
        success = not result.startswith("\u274c")
        return web.json_response({"success": success, "message": result})

    async def handle_watch(self, request: web.Request) -> web.Response:
        """Start watching a game in a new tab.

        Body: {"team": "Blues", "site": "streamed.pk"}
        or:   {"url": "https://...", "label": "Blues"}
        """
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"error": "invalid_body", "message": "Expected JSON body"},
                status=400,
            )

        # Check if this is a direct URL watch
        url = body.get("url", "").strip()
        if url:
            label = body.get("label", "").strip() or None
            logger.info(f"API: watch_url request — url={url[:60]}, label={label}")
            result = await self.keeper.watch_url(url, label)
            success = not result.startswith("\u274c")
            # Find the stream ID from the result message
            stream_id = None
            for sid in self.keeper.active_streams:
                if label and label.lower() in sid:
                    stream_id = sid
                    break
            if not stream_id and self.keeper.active_streams:
                stream_id = list(self.keeper.active_streams.keys())[-1]
            return web.json_response({
                "success": success,
                "message": result,
                "stream_id": stream_id,
                "url": url,
                "active_streams": len(self.keeper.active_streams),
            })

        team = body.get("team", "").strip()
        if not team:
            return web.json_response(
                {"error": "missing_field", "message": "'team' or 'url' field is required"},
                status=400,
            )

        site = body.get("site")
        logger.info(f"API: watch request — team={team}, site={site}")

        result = await self.keeper.watch(team, site)
        success = not result.startswith("\u274c")

        # Find the stream ID
        stream_id = None
        for sid in self.keeper.active_streams:
            if team.lower().replace(" ", "-") in sid:
                stream_id = sid
                break
        if not stream_id and self.keeper.active_streams:
            stream_id = list(self.keeper.active_streams.keys())[-1]

        return web.json_response({
            "success": success,
            "message": result,
            "stream_id": stream_id,
            "team": team,
            "site": site or self.keeper.default_site,
            "active_streams": len(self.keeper.active_streams),
        })

    async def handle_stop(self, request: web.Request) -> web.Response:
        """Stop a stream.

        Body: {"stream_id": "blues"} — stop specific stream
        or:   {"all": true} — stop all streams
        """
        try:
            body = await request.json()
        except Exception:
            body = {}

        if body.get("all"):
            result = await self.keeper.stop()
            return web.json_response({"success": True, "message": result, "active_streams": 0})

        stream_id = body.get("stream_id", "").strip()
        if not stream_id:
            return web.json_response(
                {"error": "missing_field", "message": "'stream_id' or 'all' is required"},
                status=400,
            )

        result = await self.keeper.stop(stream_id)
        success = not result.startswith("\u274c")
        return web.json_response({
            "success": success,
            "message": result,
            "active_streams": len(self.keeper.active_streams),
        })

    async def handle_reload(self, request: web.Request) -> web.Response:
        """Force reload a stream. Body: {"stream_id": "blues"}"""
        try:
            body = await request.json()
        except Exception:
            body = {}

        stream_id = body.get("stream_id", "").strip() or None
        result = await self.keeper.force_reload(stream_id)
        return web.json_response({
            "success": not result.startswith("\u274c"),
            "message": result,
        })

    async def handle_unmute(self, request: web.Request) -> web.Response:
        """Force unmute a stream. Body: {"stream_id": "blues"}"""
        try:
            body = await request.json()
        except Exception:
            body = {}

        stream_id = body.get("stream_id", "").strip() or None
        result = await self.keeper.force_unmute(stream_id)
        return web.json_response({
            "success": not result.startswith("\u274c"),
            "message": result,
        })

    async def handle_switch(self, request: web.Request) -> web.Response:
        """Switch to next stream source. Body: {"stream_id": "blues"}"""
        try:
            body = await request.json()
        except Exception:
            body = {}

        stream_id = body.get("stream_id", "").strip() or None
        result = await self.keeper.switch_source(stream_id)
        return web.json_response({
            "success": not result.startswith("\u274c"),
            "message": result,
        })

    async def handle_screenshot(self, request: web.Request) -> web.Response:
        """Take a screenshot. Optional ?stream_id=blues or screenshots all."""
        stream_id = request.query.get("stream_id")
        result = await self.keeper.take_screenshot(stream_id)

        if not result:
            return web.json_response(
                {"error": "no_stream", "message": "No active page to screenshot"},
                status=404,
            )

        # Single screenshot
        if isinstance(result, str):
            return web.FileResponse(result, headers={
                "Content-Type": "image/png",
                "Content-Disposition": f'inline; filename="{os.path.basename(result)}"',
            })

        # Multiple screenshots — return JSON with paths
        return web.json_response({
            "screenshots": [
                {"stream_id": sid, "path": path}
                for sid, path in zip(self.keeper.active_streams.keys(), result)
            ],
            "count": len(result),
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
