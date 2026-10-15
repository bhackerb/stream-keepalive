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

### Firefox playback stability (2026-10-13)

Cleanup now preserves player iframes (including `/embed/admin/` URLs) and their
containers. Health and play commands inspect cross-origin Playwright frames in
Firefox. Recovery waits for a sustained failure and uses one stage per attempt;
automatic monitoring excludes directory and off-site advertisement pages.
Direct URL startup no longer clicks arbitrary source links.

Run `python3 -m unittest test_health_monitor test_sites test_layout` for 17
hermetic checks, including a routed Firefox cross-origin canvas-video fixture.
Grok and AGY reviewed the patch. Material startup and evidence gaps were closed.
The final Grok cross-origin auto-attachment finding assumed CDP was required;
`AutoAttachTests.test_cross_origin_video_attaches_without_cdp` disproves that
assumption by executing the production loop with an embedded video and no CDP.
Repeated mirror attempts deliberately try additional mirrors; frozen-state
tracking clears when playback advances, not merely when readyState is high.

Real Viking/Bayern embeds still returned “Remove sandbox attributes on the
iframe tag” in isolated Firefox, including a top-level Admin page without
extensions or watcher scripts. This repair does not claim that source playback
is restored or that removing iframe protections will fix it.

## Regular Firefox / Chrome companion

`browser.control_mode: regular` uses your ordinary running browser and its cookies,
permissions and ad blockers. StreamKeeper never launches, copies, restarts or kills
that browser. Existing configurations without `control_mode` retain managed mode;
new example configurations select regular mode. Use `python3 install_companion.py
--enable` to build the extension, register the authenticated user-scoped native host,
back up the local config, and select regular Firefox. Restart the StreamKeeper service
once after enabling. A connected companion is required before windows can open.

Chrome: open `chrome://extensions`, enable Developer mode, choose **Load unpacked**,
and select `build/companion/chrome`. Firefox: for development open
`about:debugging#/runtime/this-firefox`, choose **Load Temporary Add-on**, and select
`build/companion/firefox/manifest.json`. Temporary Firefox installation lasts until
Firefox restarts. For permanent use, submit `build/streamkeeper-firefox.zip` to Mozilla
for **unlisted signing**, then install the resulting signed XPI. Do not disable
signature enforcement. The generated Chrome ID is stable because the committed public
key is fixed; no private key is retained. Native manifests grant only that extension
and `streamkeeper@bhackerb.local` access. No API key is placed in either extension.

The companion supports Streamed and its allowlisted player hosts. Other source-site
URLs still need the optional managed adapter; regular mode does not claim generic
control of arbitrary websites. Normal ad blocking remains the browser's responsibility;
active uBlock and observed blocked requests are separate from playback evidence.
Installing the companion does not install an ad blocker.

POST `/session` with authenticated JSON:

```json
{"action":"start","sports":["hockey","baseball"],"browser":"firefox","display":"dock","hours":12,"favorites":["St. Louis Blues","Colorado Avalanche"]}
```

Actions: `plan`, `start`, `status`, `stop`. Live games rank favorites first, then the
maximum source-level viewer count (unknown remains unknown, not zero). Channels with
no kickoff date are excluded. Later starts are tracked for a fixed 1–24 hour window
(default 12), with a 60-second catalog cycle and a bounded viewer lookup budget. Games
waiting for the live listing remain in the pending list after their scheduled time.
A listed game is not proof of video playback: inspect health for progressing video.

TV shows at most 4 games; dock shows 6 widescreen + 4 vertical. Mixed sessions reserve at least one live slot per requested sport when capacity permits.
A higher-ranked newcomer
can park a lower-ranked session window: minimize and pause its video without navigation
or reload. Returning a parked game resumes only a video that was playing when parked.
The open-window budget defaults to 20 (`session.max_open_streams`) including parked
windows; the display limits remain 4/10. Overflow is visible in the plan. The open
laptop panel is excluded from dock layouts. Existing X11/KWin placement helpers handle
the native browser windows. X11 requests placement without geometry readback; KWin
verifies placement. Cutoff changes require a >20% and >5-viewer improvement; missing counts retain
explicitly labeled last-known values. Current viewer changes alone do not reorder an unchanged set of
visible windows. Missing catalog entries preserve already visible players.

New owned streams start muted (`session.start_muted: true`) for autoplay and to avoid
multiple audio tracks. Unmute the chosen game explicitly. Health polling never refreshes
or switches sources. Explicit reload is limited to once per stream per 10 minutes.
Session expiry preserves playing streams. A session stop closes only its owned streams;
manual-owned streams and personal tabs remain. A user-closed game stays suppressed for
that session. Browser-restored tabs are reclaimed only with their saved unpredictable
URL marker. Lost ownership is surfaced and quarantined; restart the session to open
replacements. Browser and stream windows survive daemon restarts.

Validation (offline):

```sh
python3 -m unittest test_regular_browser test_session_planner test_health_monitor test_sites test_layout
node --test companion/background.test.cjs
```

The companion has no screenshot support yet. Regular-mode API screenshots return an
explicit unavailable response. Escape restores tab fill across authenticated extension
frames; iframe sandbox attributes are never removed.

Single full-size TV slots in regular-browser mode automatically use native X11
fullscreen. Firefox popup dialog windows are temporarily made normal and
undecorated because Muffin rejects fullscreen dialogs. Original window type and
decoration hints are restored when the stream returns to a tiled layout. Partial
reserved TV slots remain tiled. Fullscreen transitions require WM confirmation;
failed transitions remain retryable layout errors. No signed add-on update is needed.
Validation:63 focused Python checks, native Muffin full→half→full/idempotency,
and actual 3840×2160 TV capture with both desktop panels absent.

Sports page source changes use authenticated `/player-sources?stream_id=...` and
`/switch` with an explicit game and listed URL. The regular-browser controller
revalidates provider metadata, audits exact ownership, closes and confirms absence
before replacing the owned window, then restores its display slot/fullscreen.
No arbitrary personal tabs are adopted. A failed open retains the selected source
and resets placement signatures for the active session to retry. Switching a source
starts the replacement muted. This uses the existing signed companion unchanged.

### Confirmed audio and buffering health

Authenticated `POST /audio` accepts exactly one owned `stream_id`, a boolean
`muted`, and optional finite `volume` from 0 to 1. For example:
`{"stream_id":"game-id","muted":false,"volume":0.5}`. Success requires
confirmation from each permitted player frame and the browser's tab mute state.
Failed or partial confirmations return `success: false`; some frames or tab audio
may already have changed. Changes are not atomic, and rollback after ownership
loss would risk touching an unowned tab. Refresh health before retrying. Success
requires at least one video element, so an audio-only advertisement cannot count
as player confirmation. Audio commands retain the existing startup mute policy;
replacement sources still start according to session configuration. The daemon
never changes personal or unowned tabs.

Authenticated `POST /resume` accepts only `{"stream_id":"game-id"}` for one
visible, connected, owned stream. It sends the signed companion's existing play
operation once and waits up to six seconds for fresh primary-video media time
and, when available, decoded frames to advance. A command acknowledgment alone
never confirms playback. Blocked or unconfirmed playback returns HTTP 409 with
`success: false` and guidance to click Play in that stream window. Resume keeps
the source, window, mute, and volume settings.

If a freshly playing stream pauses immediately after `POST /audio` unmutes it,
the daemon observes fresh health for up to four seconds and attempts the same
bounded play operation once. An already paused stream stays paused. Applied audio
settings still return `confirmed: true`; failed playback recovery additionally
returns `playback_confirmed: false` and an actionable `playback_warning`.
The existing signed companion needs no update for resume or unmute protection.

`GET /status` exposes `audio_capabilities` per browser. Mute and volume require
companion 0.2.0 advertising `audio-v1`; the signed 0.1.0 add-on continues its
existing Unmute command and receives no unsupported audio commands. Build the
updated package with `python3 build_companion.py`. Firefox requires Mozilla
unlisted signing of `build/streamkeeper-firefox.zip` before permanent installation.
The build does not sign, install, or replace any existing add-on.

The controller reports `BUFFERING` only after it observed media time and, when
available, decoded frames advance, followed by at least 15 seconds of reported
stalling. Initial loading, pauses, old evidence, and ended playback do not qualify.
Visible session streams can use the existing bounded recovery cascade after 90
seconds of sustained fresh buffering evidence and progress within the previous
five minutes: one reload, then up to two catalog alternatives. Ownership audits,
close confirmation, and inventory drain guard source replacement. Progress is
cleared by reload, source replacement, and reconnection, so initial loading after
those actions needs new playback evidence. Source URLs remain restricted to the
existing companion allowlist; generic managed-browser site support does not imply
regular-browser support.

In companion0.2.0, the legacy Unmute action intentionally also clears the owned
browser tab's mute flag. Otherwise the newly introduced tab mute could keep an
explicitly unmuted video silent. It retains the existing permissive frame message
behavior; the stricter confirmed API applies only to the new audio control.

### Recover a live display without changing the draft

Authenticated `POST /restore-layout` with `{ "display": "tv" }` (or a configured monitor name) reapplies only that active session's current placements and tab fill. It never opens/closes games, applies a draft, or changes sources. Gwaihir exposes this on each monitor header.

The existing sixty-second session tick maintains a single full-size X11 TV slot using actual fullscreen and geometry readback. Half slots, parked/transferring/lost games, and disconnected companions are skipped. Escape sets companion fill=false and suspends automatic focus repair; Restore live focus re-enables it. F11/window-manager exits while fill remains enabled are treated as focus drift. No companion update is required.

Regular-browser grid placement now requests borderless windows: X11 removes the owned window's native decorations while retaining its original type/hints for restoration; KWin sets noBorder on the exact identified stream windows and verifies the resulting geometry. This does not enter monitor fullscreen for tiled streams or modify regular browser profiles. A Firefox toolbar rendered inside a popup is separate from native window decoration and is not hidden by this change.
Set `layout.borderless: false` in the native controller configuration to opt out on the next placement/Restore live layout. Regular windows default to borderless; managed placement keeps its previous decoration behavior.
