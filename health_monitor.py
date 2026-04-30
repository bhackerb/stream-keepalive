"""
Health Monitor — Polls the HTML5 <video> element for stream health.

Monitors:
- readyState: Is enough data buffered?
- currentTime: Is playback advancing? (frozen stream detection)
- paused/ended: Did playback stop unexpectedly?
- muted/volume: Is audio still on?
- error: Did the video element encounter an error?
- Network activity: Are new segments being fetched?
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from playwright.async_api import Page

logger = logging.getLogger("stream-keeper.health")


class StreamState(Enum):
    UNKNOWN = "unknown"
    LOADING = "loading"
    PLAYING = "playing"
    STALLED = "stalled"
    FROZEN = "frozen"
    PAUSED = "paused"
    ENDED = "ended"
    ERROR = "error"
    NO_VIDEO = "no_video"
    AUDIO_LOST = "audio_lost"


@dataclass
class HealthSnapshot:
    """Point-in-time health reading from the video element."""
    timestamp: float = 0.0
    state: StreamState = StreamState.UNKNOWN
    ready_state: int = 0
    current_time: float = 0.0
    paused: bool = False
    ended: bool = False
    muted: bool = False
    volume: float = 1.0
    video_width: int = 0
    video_height: int = 0
    buffered_end: float = 0.0
    error_code: Optional[int] = None
    error_message: str = ""
    has_audio_tracks: bool = True
    network_state: int = 0

    @property
    def is_healthy(self) -> bool:
        return self.state == StreamState.PLAYING

    @property
    def has_audio(self) -> bool:
        return not self.muted and self.volume > 0 and self.has_audio_tracks


@dataclass
class HealthHistory:
    """Tracks health over time for stall/freeze detection."""
    snapshots: list = field(default_factory=list)
    max_snapshots: int = 60  # ~5 min at 5s intervals
    last_advancing_time: float = 0.0
    last_current_time: float = 0.0
    stall_start: Optional[float] = None
    recovery_count: int = 0
    last_recovery: float = 0.0

    def add(self, snap: HealthSnapshot):
        self.snapshots.append(snap)
        if len(self.snapshots) > self.max_snapshots:
            self.snapshots.pop(0)

        # Track if currentTime is advancing
        if snap.current_time > self.last_current_time + 0.5:
            self.last_advancing_time = snap.timestamp
            self.stall_start = None
        elif snap.state not in (StreamState.NO_VIDEO, StreamState.LOADING, StreamState.UNKNOWN):
            if self.stall_start is None:
                self.stall_start = snap.timestamp

        self.last_current_time = snap.current_time

    @property
    def stall_duration(self) -> float:
        if self.stall_start is None:
            return 0.0
        return time.time() - self.stall_start

    @property
    def seconds_since_recovery(self) -> float:
        if self.last_recovery == 0:
            return float("inf")
        return time.time() - self.last_recovery


# JavaScript injected into the page to read video element state
HEALTH_CHECK_JS = """() => {
    // Find the video element — check main page and iframes
    let video = document.querySelector('video');

    // If not found, search inside iframes (same-origin only)
    if (!video) {
        const iframes = document.querySelectorAll('iframe');
        for (const iframe of iframes) {
            try {
                const iframeDoc = iframe.contentDocument || iframe.contentWindow?.document;
                if (iframeDoc) {
                    video = iframeDoc.querySelector('video');
                    if (video) break;
                }
            } catch (e) {
                // Cross-origin iframe, can't access
            }
        }
    }

    if (!video) {
        return { found: false };
    }

    // Read all relevant properties
    const result = {
        found: true,
        readyState: video.readyState,
        currentTime: video.currentTime,
        duration: video.duration,
        paused: video.paused,
        ended: video.ended,
        muted: video.muted,
        volume: video.volume,
        videoWidth: video.videoWidth,
        videoHeight: video.videoHeight,
        networkState: video.networkState,
        error: null,
        bufferedEnd: 0,
        hasAudioTracks: true,
    };

    // Check for errors
    if (video.error) {
        result.error = {
            code: video.error.code,
            message: video.error.message || 'Unknown error'
        };
    }

    // Check buffered ranges
    if (video.buffered && video.buffered.length > 0) {
        result.bufferedEnd = video.buffered.end(video.buffered.length - 1);
    }

    // Check audio tracks if API available
    if (video.audioTracks) {
        result.hasAudioTracks = video.audioTracks.length > 0;
    }

    // Check via Web Audio API if audio is actually producing sound
    // (This is a best-effort check — may not work on all sites)
    try {
        if (window.__streamkeeper_audio_ctx) {
            const analyser = window.__streamkeeper_analyser;
            const data = new Uint8Array(analyser.frequencyBinCount);
            analyser.getByteFrequencyData(data);
            const avg = data.reduce((a, b) => a + b, 0) / data.length;
            result.audioLevel = avg;
        }
    } catch (e) {
        // Audio context not set up yet or cross-origin
    }

    return result;
}"""

# JavaScript to set up Web Audio API monitoring on the video element
SETUP_AUDIO_MONITOR_JS = """() => {
    const video = document.querySelector('video');
    if (!video || window.__streamkeeper_audio_ctx) return false;

    try {
        const ctx = new AudioContext();
        const source = ctx.createMediaElementSource(video);
        const analyser = ctx.createAnalyser();
        analyser.fftSize = 256;
        source.connect(analyser);
        analyser.connect(ctx.destination);  // Still output audio to speakers

        window.__streamkeeper_audio_ctx = ctx;
        window.__streamkeeper_analyser = analyser;
        return true;
    } catch (e) {
        // Cross-origin or already connected
        return false;
    }
}"""


class HealthMonitor:
    """Monitors stream health by polling the HTML5 video element."""

    def __init__(self, config: dict):
        self.config = config
        self.history = HealthHistory()
        self.poll_interval = config.get("health", {}).get("poll_interval_seconds", 5)
        self.stall_threshold = config.get("health", {}).get("stall_threshold_seconds", 15)
        self._audio_monitor_setup = False
        self._monitoring = False
        self._page: Optional[Page] = None

    async def check_health_page_level(self, page: Page) -> HealthSnapshot:
        """Fallback health check when video element is unreachable (cross-origin iframe).

        Instead of polling the <video> element directly, checks page-level liveness:
        is the tab still navigable, is the URL sane, is the title still present?
        """
        snap = HealthSnapshot(timestamp=time.time())
        try:
            title = await page.title()
            url = page.url
            if "chrome-error" in url or url == "about:blank":
                snap.state = StreamState.ERROR
                snap.error_message = f"Page navigated to error/blank: {url}"
            elif not title:
                snap.state = StreamState.LOADING
            else:
                snap.state = StreamState.PLAYING
        except Exception as e:
            snap.state = StreamState.ERROR
            snap.error_message = f"Page unreachable: {e}"
        self.history.add(snap)
        return snap

    async def check_health(self, page: Page) -> HealthSnapshot:
        """Take a single health snapshot from the video element."""
        snap = HealthSnapshot(timestamp=time.time())

        try:
            result = await page.evaluate(HEALTH_CHECK_JS)
        except Exception as e:
            logger.warning(f"Health check JS failed: {e}")
            snap.state = StreamState.ERROR
            snap.error_message = str(e)
            self.history.add(snap)
            return snap

        if not result.get("found"):
            snap.state = StreamState.NO_VIDEO
            self.history.add(snap)
            return snap

        # Map result to snapshot
        snap.ready_state = result.get("readyState", 0)
        snap.current_time = result.get("currentTime", 0)
        snap.paused = result.get("paused", False)
        snap.ended = result.get("ended", False)
        snap.muted = result.get("muted", False)
        snap.volume = result.get("volume", 1.0)
        snap.video_width = result.get("videoWidth", 0)
        snap.video_height = result.get("videoHeight", 0)
        snap.network_state = result.get("networkState", 0)
        snap.buffered_end = result.get("bufferedEnd", 0)
        snap.has_audio_tracks = result.get("hasAudioTracks", True)

        if result.get("error"):
            snap.error_code = result["error"].get("code")
            snap.error_message = result["error"].get("message", "")

        # Determine state
        snap.state = self._determine_state(snap)

        self.history.add(snap)
        return snap

    def _determine_state(self, snap: HealthSnapshot) -> StreamState:
        """Determine the overall stream state from a snapshot."""
        if snap.error_code:
            return StreamState.ERROR
        if snap.ended:
            return StreamState.ENDED
        if snap.paused:
            return StreamState.PAUSED
        if snap.ready_state < 2:
            return StreamState.LOADING
        if snap.ready_state < 3:
            return StreamState.STALLED

        # Check for frozen stream (currentTime not advancing)
        if self.history.stall_duration > self.stall_threshold:
            return StreamState.FROZEN

        # Check audio health
        if snap.muted or snap.volume == 0:
            return StreamState.AUDIO_LOST

        return StreamState.PLAYING

    async def setup_audio_monitor(self, page: Page) -> bool:
        """Set up Web Audio API monitoring for silent stream detection."""
        if self._audio_monitor_setup:
            return True
        try:
            result = await page.evaluate(SETUP_AUDIO_MONITOR_JS)
            self._audio_monitor_setup = result
            if result:
                logger.info("Audio monitor (Web Audio API) set up successfully")
            return result
        except Exception as e:
            logger.debug(f"Could not set up audio monitor: {e}")
            return False

    async def force_unmute(self, page: Page) -> bool:
        """Force the video element to unmute and set volume to max."""
        try:
            result = await page.evaluate("""() => {
                let video = document.querySelector('video');
                if (!video) {
                    const iframes = document.querySelectorAll('iframe');
                    for (const iframe of iframes) {
                        try {
                            const doc = iframe.contentDocument || iframe.contentWindow?.document;
                            if (doc) {
                                video = doc.querySelector('video');
                                if (video) break;
                            }
                        } catch (e) {}
                    }
                }
                if (!video) return false;
                video.muted = false;
                video.volume = 1.0;
                return true;
            }""")
            if result:
                logger.info("Forced video unmute + volume 1.0")
            return result
        except Exception as e:
            logger.warning(f"Force unmute failed: {e}")
            return False

    async def force_play(self, page: Page) -> bool:
        """Force the video element to play."""
        try:
            result = await page.evaluate("""() => {
                let video = document.querySelector('video');
                if (!video) {
                    const iframes = document.querySelectorAll('iframe');
                    for (const iframe of iframes) {
                        try {
                            const doc = iframe.contentDocument || iframe.contentWindow?.document;
                            if (doc) {
                                video = doc.querySelector('video');
                                if (video) break;
                            }
                        } catch (e) {}
                    }
                }
                if (!video) return false;
                video.play().catch(() => {});
                video.muted = false;
                video.volume = 1.0;
                return true;
            }""")
            if result:
                logger.info("Forced video play + unmute")
            return result
        except Exception as e:
            logger.warning(f"Force play failed: {e}")
            return False

    def needs_recovery(self) -> bool:
        """Check if the stream needs recovery action."""
        if not self.history.snapshots:
            return False
        latest = self.history.snapshots[-1]
        return latest.state in (
            StreamState.STALLED,
            StreamState.FROZEN,
            StreamState.ERROR,
            StreamState.ENDED,
            StreamState.PAUSED,
        )

    def get_recovery_reason(self) -> str:
        """Get human-readable reason for why recovery is needed."""
        if not self.history.snapshots:
            return "No health data"
        latest = self.history.snapshots[-1]
        reasons = {
            StreamState.STALLED: f"Stream stalled (readyState={latest.ready_state})",
            StreamState.FROZEN: f"Stream frozen for {self.history.stall_duration:.0f}s (currentTime stuck at {latest.current_time:.1f})",
            StreamState.ERROR: f"Video error: code={latest.error_code} {latest.error_message}",
            StreamState.ENDED: "Stream ended unexpectedly",
            StreamState.PAUSED: "Video paused unexpectedly",
            StreamState.AUDIO_LOST: f"Audio lost (muted={latest.muted}, volume={latest.volume})",
            StreamState.NO_VIDEO: "No video element found on page",
        }
        return reasons.get(latest.state, f"Unknown issue: {latest.state}")

    @property
    def stats(self) -> dict:
        latest = self.history.snapshots[-1] if self.history.snapshots else None
        return {
            "state": latest.state.value if latest else "unknown",
            "current_time": f"{latest.current_time:.1f}s" if latest else "N/A",
            "ready_state": latest.ready_state if latest else -1,
            "resolution": f"{latest.video_width}x{latest.video_height}" if latest else "N/A",
            "audio": "OK" if (latest and latest.has_audio) else "LOST",
            "muted": latest.muted if latest else None,
            "volume": latest.volume if latest else None,
            "stall_duration": f"{self.history.stall_duration:.0f}s",
            "recovery_count": self.history.recovery_count,
            "buffered_to": f"{latest.buffered_end:.1f}s" if latest else "N/A",
        }
