"""HTTP API for StreamKeeper — allows remote control from Mithrandir or other devices."""

import asyncio
import hmac
import json
import logging
import math
import os
import time
from typing import Optional, TYPE_CHECKING

from aiohttp import web

from regular_browser import CatalogUnavailable

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
    return hmac.compare_digest(token or "", api_key or "")


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
        self.app.router.add_post("/settings", self.handle_settings)
        self.app.router.add_get("/health", self.handle_health)
        self.app.router.add_get("/browser/bridge", self.handle_bridge)
        self.app.router.add_post("/session", self.handle_session)
        self.app.router.add_post("/session-layout", self.handle_session_layout)
        self.app.router.add_get("/status", self.handle_status)
        self.app.router.add_post("/watch", self.handle_watch)
        self.app.router.add_post("/keepalive", self.handle_keepalive)
        self.app.router.add_post("/stop", self.handle_stop)
        self.app.router.add_post("/reload", self.handle_reload)
        self.app.router.add_post("/unmute", self.handle_unmute)
        self.app.router.add_post("/audio", self.handle_audio)
        self.app.router.add_post("/resume", self.handle_resume)
        self.app.router.add_post("/seek", self.handle_seek)
        self.app.router.add_post("/restore-layout", self.handle_restore_layout)
        self.app.router.add_post("/switch", self.handle_switch)
        self.app.router.add_get("/player-sources", self.handle_player_sources)
        self.app.router.add_get("/screenshot", self.handle_screenshot)
        self.app.router.add_post("/layout", self.handle_layout)
        self.app.router.add_post("/tab-view", self.handle_tab_view)

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        # Health endpoint is unauthenticated
        if request.path == "/health":
            return await handler(request)

        # Every control route fails closed without a key, including legacy dev configuration.
        if not self.api_key or self.api_key == "YOUR_API_KEY":
            return web.json_response({"error": "API authentication required"}, status=503)
        if request.path == "/browser/bridge" and request.remote not in ("127.0.0.1", "::1"):
            return web.json_response({"error": "Native bridge is local only"}, status=403)

        # All other endpoints require auth
        if not _check_auth(request, self.api_key):
            return web.json_response(
                {"error": "unauthorized", "message": "Invalid or missing Bearer token"},
                status=401,
            )

        return await handler(request)

    async def handle_bridge(self, request):
        if not hasattr(self.keeper, "bridge"):
            return web.json_response({"error": "Enable regular browser mode first"}, status=409)
        return await self.keeper.bridge(request)

    async def handle_session(self, request):
        if not hasattr(self.keeper, "session_action"):
            return web.json_response({"error": "Session mode requires regular browser control"}, status=409)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Expected session object")
            return web.json_response(await self.keeper.session_action(body))
        except CatalogUnavailable as exc:
            logger.warning("Session catalog unavailable: %s", exc)
            return web.json_response({"success": False, "message": f"{exc}; existing streams preserved"}, status=502)
        except (ValueError, TypeError) as exc:
            return web.json_response({"success": False, "message": str(exc)}, status=400)
        except Exception:
            logger.exception("Session request failed")
            return web.json_response({"success": False, "message": "Session control failed; existing streams preserved"}, status=502)

    async def handle_session_layout(self,request):
        if not hasattr(self.keeper,"set_session_layout"):
            return web.json_response({"success":False,"message":"Enable regular browser mode first"},status=409)
        try:return web.json_response(await self.keeper.set_session_layout(await request.json()))
        except (ValueError,TypeError)as exc:return web.json_response({"success":False,"message":str(exc)},status=400)
        except Exception:
            logger.exception("Manual layout failed")
            return web.json_response({"success":False,"message":"Manual layout failed; inspect current stream status"},status=502)

    async def handle_settings(self,request):
        if not hasattr(self.keeper,'settings_action'):return web.json_response({'success':False,'message':'Local settings require regular browser mode'},status=409)
        try:return web.json_response(await self.keeper.settings_action(await request.json()))
        except (ValueError,TypeError)as exc:return web.json_response({'success':False,'message':str(exc)},status=400)

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
        if hasattr(self.keeper,"refresh_displays"):await self.keeper.refresh_displays()
        stream_id = request.query.get("stream_id")
        capabilities = {
            "browser_engine": self.keeper.browser_engine,
            "displays": self.config.get("layout", {}).get("displays", {}),
            "layout": self.keeper.layout_status,
            "ads": self.keeper.ad_handler.stats,
            **(self.keeper.extra_status() if hasattr(self.keeper, "extra_status") else {}),
        }

        if not self.keeper.active_streams:
            return web.json_response({
                "is_watching": False,
                "streams": [],
                **capabilities,
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
                **capabilities,
                "ads": {
                    "requests_blocked": ads["requests_blocked"],
                    **ads,
                },
            })

        # All streams
        streams = [self._stream_to_dict(s) for s in self.keeper.active_streams.values()]
        return web.json_response({
            "is_watching": True,
            "active_count": len(streams),
            "max_streams": self.keeper.max_streams,
            "streams": streams,
            **capabilities,
            "ads": {
                "requests_blocked": ads["requests_blocked"],
                **ads,
            },
        })

    def _stream_to_dict(self, stream) -> dict:
        """Convert an ActiveStream to a JSON-serializable dict."""
        health = stream.health_monitor.stats
        return {
            "stream_id": stream.id,
            "browser": getattr(stream,"browser",self.keeper.browser_engine),
            "audio_control": bool(hasattr(self.keeper,"audio_capabilities") and self.keeper.audio_capabilities().get(getattr(stream,"browser",self.keeper.browser_engine),{}).get("available")),
            "team": stream.team,
            "site": stream.site,
            "url": stream.url,
            "started_at": stream.started_at,
            "parked": getattr(stream, "parked", False),
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
                "duration": health.get("duration"),
            },
        }

    async def handle_layout(self, request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Expected JSON object")
            ids = body.get("stream_ids")
            if ids is not None and (not isinstance(ids, list) or not all(isinstance(s, str) for s in ids)):
                raise ValueError("stream_ids must be a list of strings")
            display, mode = body.get("display", "tv"), body.get("mode", "grid")
            if not isinstance(display, str) or not isinstance(mode, str):
                raise ValueError("display and mode must be strings")
            result = await self.keeper.arrange_streams(display, mode, ids)
            return web.json_response(result, status=200 if result["success"] else 400)
        except (ValueError, TypeError):
            return web.json_response({"success": False, "message": "Invalid layout request"}, status=400)

    async def handle_tab_view(self, request):
        try:
            body = await request.json()
            if not isinstance(body, dict) or type(body.get("enabled", True)) is not bool:
                raise ValueError("enabled must be boolean")
            sid = body.get("stream_id")
            if sid is not None and not isinstance(sid, str):
                raise ValueError("stream_id must be a string")
            return web.json_response(await self.keeper.set_tab_view(sid, body.get("enabled", True)))
        except (ValueError, TypeError):
            return web.json_response({"success": False, "message": "Invalid tab-view request"}, status=400)

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
        try:
            result = await self.keeper.force_reload(stream_id)
        except ValueError as exc:
            # Companion disconnected or stream unknown: a client error, not a server fault.
            return web.json_response({"success": False, "message": str(exc)}, status=409)
        except TimeoutError:
            return web.json_response({"success": False, "message": "Browser did not answer the reload in time"}, status=504)
        return web.json_response({
            "success": not result.startswith("\u274c"),
            "message": result,
        })

    async def handle_restore_layout(self,request):
        if not hasattr(self.keeper,'restore_live_layout'):return web.json_response({'success':False,'message':'Restore requires regular browser control'},status=409)
        try:
            body=await request.json()
            if not isinstance(body,dict)or not isinstance(body.get('display'),str):raise ValueError('display is required')
            result=await self.keeper.restore_live_layout(body['display'])
            return web.json_response(result,status=200 if result.get('success') else 409)
        except (ValueError,TimeoutError)as exc:return web.json_response({'success':False,'message':str(exc)},status=409)
        except Exception:
            logger.exception('Live layout restore failed')
            return web.json_response({'success':False,'message':'Live layout could not be restored; refresh status and retry'},status=409)

    async def handle_seek(self,request):
        try:
            body=await request.json()
            if not isinstance(body,dict)or not isinstance(body.get('stream_id'),str):raise ValueError('An exact stream_id is required')
        except (ValueError,TypeError)as exc:
            return web.json_response({'success':False,'message':str(exc)or'Invalid request'},status=400)
        if not hasattr(self.keeper,'seek'):
            return web.json_response({'success':False,'message':'Seek requires regular browser control'},status=409)
        try:
            return web.json_response(await self.keeper.seek(body['stream_id'],body.get('to'),body.get('delta')))
        except (ValueError,TimeoutError)as exc:
            return web.json_response({'success':False,'message':str(exc)},status=409)

    async def handle_resume(self,request):
        try:
            body=await request.json()
            if not isinstance(body,dict)or set(body)!={'stream_id'}or not isinstance(body.get('stream_id'),str)or not body['stream_id'].strip():
                raise ValueError('An exact stream_id is required')
        except (ValueError,TypeError):
            return web.json_response({'success':False,'playback_confirmed':False,'message':'An exact stream_id is required'},status=400)
        if not hasattr(self.keeper,'resume_playback'):
            return web.json_response({'success':False,'playback_confirmed':False,'message':'Resume requires regular browser control'},status=409)
        try:
            result=await self.keeper.resume_playback(body['stream_id'])
            return web.json_response(result,status=200 if result.get('success') else 409)
        except (ValueError,TimeoutError)as exc:
            return web.json_response({'success':False,'playback_confirmed':False,'message':str(exc)},status=409)
        except Exception:
            logger.exception('Playback resume failed')
            return web.json_response({'success':False,'playback_confirmed':False,'message':'Playback could not be confirmed; click Play in this stream window and refresh status'},status=409)

    async def handle_audio(self,request):
        if not hasattr(self.keeper,'set_audio'):
            return web.json_response({'success':False,'message':'Per-stream audio requires regular browser control'},status=409)
        try:
            body=await request.json()
            if not isinstance(body,dict)or not isinstance(body.get('stream_id'),str)or not body['stream_id']or type(body.get('muted'))is not bool:
                raise ValueError('stream_id and boolean muted are required')
            if 'volume'in body and (type(body['volume'])not in(int,float)or not math.isfinite(body['volume'])or not 0<=body['volume']<=1):raise ValueError('volume must be a finite number from 0 to 1')
            return web.json_response(await self.keeper.set_audio(body['stream_id'],body['muted'],body.get('volume')))
        except NotImplementedError as exc:return web.json_response({'success':False,'confirmed':False,'message':str(exc)},status=409)
        except (ValueError,TypeError)as exc:return web.json_response({'success':False,'confirmed':False,'message':str(exc)},status=400)
        except Exception:
            logger.exception('Audio control failed')
            return web.json_response({'success':False,'confirmed':False,'message':'Audio confirmation unavailable; some settings may have changed. Refresh status before retrying'},status=502)

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

        if not isinstance(body,dict)or not isinstance(body.get('stream_id',''),str):return web.json_response({'error':'Invalid game'},status=400)
        stream_id = body.get("stream_id", "").strip() or None
        if 'user_source'in body and type(body['user_source'])is not bool:return web.json_response({'error':'Invalid supplied URL selection'},status=400)
        if body.get('user_source')is True and not hasattr(self.keeper,'player_sources'):return web.json_response({'error':'Supplied URLs require regular browser mode'},status=400)
        if hasattr(self.keeper,'player_sources'):
            result = await self.keeper.switch_source(stream_id,body.get('url'),**({'user_source':True}if body.get('user_source')is True else {}))
        elif body.get('url') is not None:return web.json_response({'error':'Source picker requires regular browser mode'},status=400)
        else:result = await self.keeper.switch_source(stream_id)
        return web.json_response({
            "success": not result.startswith("\u274c"),
            "message": result,
        })

    async def handle_player_sources(self,request):
        if not hasattr(self.keeper,'player_sources'):return web.json_response({'error':'Source picker requires regular browser mode'},status=400)
        try:return web.json_response(await self.keeper.player_sources(request.query.get('stream_id'),**({'include_catalog':False}if request.query.get('catalog')=='0'else {})))
        except (ValueError,TimeoutError)as e:return web.json_response({'error':str(e)or'Source lookup timed out; try again'},status=400)

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
