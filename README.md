# StreamKeeper 🏒📺

**A Mithrandir agent for keeping sports streams alive.**

StreamKeeper is a Playwright-based Python automation tool that navigates to your game stream, blocks ads, monitors stream health (video stalls, audio dropout, crashes), and auto-recovers — all controllable via Discord.

## Architecture

```
StreamKeeper (Mithrandir Agent: "Watcher")
├── Discord Bot Interface     # Command & control via Discord
├── Stream Orchestrator       # Main event loop & recovery logic
├── Site Drivers              # Bespoke drivers plus generic domain auto-detection
├── Health Monitor            # Video element polling, audio checks
├── Ad Handler                # Network interception + overlay dismissal
└── Playwright Browser        # Headed Firefox by default; Chromium opt-in
```

## Features

- **Firefox:** `browser.engine: firefox` launches Playwright's patched Firefox with a separate persistent profile and verified network blocking. Install its binary with `python3 -m playwright install firefox`. Regular desktop Firefox cannot be attached via Chromium CDP; loading uBlock via Chromium extension flags is unsupported in Firefox.
- **Fill the tab:** `browser.fill_tab: true` expands the player and its iframe chain inside the existing tab in both browsers. Video nodes and player controls remain intact. Escape restores the page until manually re-enabled via `POST /tab-view`. This is independent of OS fullscreen, casting, and site theater buttons.
- **Display layouts:** `POST /layout` takes `display` (`tv`, `widescreen`, `vertical`, `dock`), `mode` (`grid`, `side-by-side`, `focus`) and selected `stream_ids`. TV supports four, widescreen six, vertical four, and dock six plus four. Focus selects one stream; side-by-side selects two. Configure actual desktop coordinates in `layout.displays`; enable `auto_arrange` to reflow new/stopped streams. Selected streams must occupy separate windows; existing tabbed streams are rejected before movement.
- **Desktop support:** Chromium uses CDP on X11; Firefox uses EWMH/X11 with python-xlib. KDE Wayland uses `layout.backend: kwin` with qdbus6/dbus-next for either browser, verifies the resulting geometry, and unloads its temporary script. Other Wayland compositors and Windows native tiling are not implemented.
- **Adblock evidence:** Owned-browser routes run a reserved `.invalid` canary at startup. Status reports `network_enabled` and `network_verified`. Attached browsers keep the existing low-latency path without Python interception; their uBlock state is explicitly unverified rather than assumed. `extension_verified` remains false until extension-specific verification is implemented.

- **Discord-controlled**: `!watch Blues` to start, `!status` to check, `!switch` to change streams
- **Ad blocking**: Verified network route interception plus full uBlock Origin in the deployed managed Firefox profiles. New profiles need the Mozilla package installed separately; optional Chromium extension support remains available.
- **Stream health monitoring**: Polls HTML5 `<video>` element for stalls, freezes, audio loss
- **Auto-recovery**: Graduated recovery — unmute → replay → reload iframe → reload page → try next mirror
- **Audio watchdog**: Detects muted/silent streams and forces audio back on
- **Status notifications**: Posts to Discord when stream dies, recovers, or needs manual intervention

## Prerequisites

- Python 3.11+
- Playwright (`pip install playwright && playwright install firefox`)
- discord.py
- Optional for Chromium: uBlock Origin (extracted extension directory)
- A Discord bot token

## Setup

1. **Install dependencies:**
   ```bash
   pip install playwright discord.py pyyaml --break-system-packages
   playwright install firefox
   ```

2. **Optional Chromium extension setup:**
   ```bash
   # Download from GitHub releases or extract from your Chrome profile
   # Place the extracted extension in ./extensions/ublock-origin/
   # The directory should contain a manifest.json
   ```

3. **Configure:**
   ```bash
   cp config.example.yaml config.yaml
   # Edit config.yaml with your Discord bot token and preferences
   ```

4. **Run:**
   ```bash
   python stream_keeper.py
   ```

## Discord Commands

| Command | Description |
|---------|-------------|
| `!watch <team>` | Find and start watching a game for the given team |
| `!watch <team> --site onhockey` | Use a specific site |
| `!status` | Show current stream health stats |
| `!switch` | Try the next available mirror/stream |
| `!reload` | Force reload the current stream |
| `!unmute` | Force unmute the stream |
| `!stop` | Stop watching and close browser |
| `!screenshot` | Take a screenshot of current stream state |

## How It Works

### Stream Discovery
1. Navigates to streamed.pk/onhockey.tv, or attaches to a supported stream tab already open in Chromium
2. Searches game listings for your team name
3. Clicks into the game page
4. Dismisses ad overlays
5. Locates the video player (iframe or direct `<video>` element)

Generic auto-detection currently recognizes VIPRow/VIPBox/VIPLeague, LiveTV,
EmbedSports, Sportsurge, StreamEast, CrackStreams, MethStreams, BuffStreams,
and DaddyLive domain aliases. Start Chromium with remote debugging enabled so
StreamKeeper can attach to an existing tab. Cross-origin iframe contents cannot
always be inspected directly; the generic driver navigates to discovered iframe
sources when possible, but site DOM changes may still require manual selection.

### Health Monitoring Loop (every 5 seconds)
1. Check `video.readyState` — is there enough data to play?
2. Check `video.currentTime` — is it advancing? (frozen stream detection)
3. Check `video.paused` / `video.ended` — unexpected stop?
4. Check `video.muted` / `video.volume` — audio still on?
5. Check for error events on the video element
6. Check for ad overlays that appeared on top of the player

### Recovery Cascade
1. **Soft fix**: `video.play()`, unmute, set volume to 1.0
2. **Iframe reload**: Find the stream iframe and reload its src
3. **Page reload**: Reload the entire page and re-navigate to stream
4. **Mirror switch**: Go back to game page and try next stream link
5. **Full restart**: Navigate from scratch back to the game listing
6. **Give up**: Notify Discord that manual intervention is needed

## Mithrandir Integration

StreamKeeper is designed as the **Watcher** agent in your Mithrandir roster.
It can be triggered by other agents or via Discord approval flows.

## Notes

### Firefox add-ons

The watcher restores Mozilla add-on discovery, installation and automatic extension updates instead of retaining Playwright's test-only add-on settings. Open `about:addons` to manage extensions, or visit [Mozilla's uBlock Origin page](https://addons.mozilla.org/en-US/firefox/addon/ublock-origin/).

On 2026-10-13, Mozilla-hosted uBlock Origin 1.75.0 was installed in both managed profiles as `extensions/uBlock0@raymondhill.net.xpi`. The downloaded package matched the SHA256 published by Mozilla's API; each live profile reports active, not disabled, and `signedState: 2`. An isolated A/B browser check with the watcher's network route installed blocked a script matching a bundled EasyList rule while allowing a player script. The existing API `extension_verified` field is still conservative: this verification is separate from its automatic network canary.

### Device mapping observed 2026-10-13

bhbserv (X11): TV `HDMI-0`, 3840×2160 at (0,0), usable work area 3840×2080. Chromium runs at scale 2; configure `layout.cdp_scale: 2` to convert physical display pixels to CDP device-independent coordinates. Native two-window verification measured 960×1040 DIP windows covering the usable TV area, with responsive page viewports.

ben-laptop (KDE Wayland): widescreen `DP-6`, 3440×1440 at (1152,383); vertical `DP-4`, 1152×2048 at (0,0). The scaled open laptop panel `eDP-1` stays available for controls. These are desktop coordinates, not assumed hardware arrangement. Recheck after docking or changing scale/resolution.

On this laptop, Playwright Firefox 146's native Wayland binary crashes with a `wp_image_description_v1` protocol mismatch. Set `browser.firefox_display_backend: x11` to use XWayland; the KWin backend still controls its windows. A headed two-window Firefox smoke verified actual side-by-side geometry on the widescreen to within one desktop pixel. The verification windows used an isolated temporary profile and were closed afterward.

### Verification

`python3 -m unittest -v test_sites test_layout` is hermetic. `python3 verify_browser.py firefox` (or `chromium`) explicitly launches a browser against synthetic routed pages; it checks real installed-route ad blocking, nested iframe tab fill, resize and Escape/manual restore. It makes no streaming-site or LLM calls. Live sources and actual media codecs can still require site-specific troubleshooting.

- Runs in **headed mode** (not headless) — you need a display since you're watching the game
- Uses persistent browser context so extension settings and cookies survive restarts
- Site-specific selectors will need periodic tuning as these sites change their DOM
- Use `playwright codegen <url>` to quickly capture updated selectors
