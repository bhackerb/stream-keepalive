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
└── Playwright Browser        # Headed Chromium w/ uBlock Origin
```

## Features

- **Discord-controlled**: `!watch Blues` to start, `!status` to check, `!switch` to change streams
- **Ad blocking**: Dual-layer — network route interception + uBlock Origin extension
- **Stream health monitoring**: Polls HTML5 `<video>` element for stalls, freezes, audio loss
- **Auto-recovery**: Graduated recovery — unmute → replay → reload iframe → reload page → try next mirror
- **Audio watchdog**: Detects muted/silent streams and forces audio back on
- **Status notifications**: Posts to Discord when stream dies, recovers, or needs manual intervention

## Prerequisites

- Python 3.11+
- Playwright (`pip install playwright && playwright install chromium`)
- discord.py
- uBlock Origin (extracted extension directory)
- A Discord bot token

## Setup

1. **Install dependencies:**
   ```bash
   pip install playwright discord.py pyyaml --break-system-packages
   playwright install chromium
   ```

2. **Get uBlock Origin:**
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

- Runs in **headed mode** (not headless) — you need a display since you're watching the game
- Uses persistent browser context so extension settings and cookies survive restarts
- Site-specific selectors will need periodic tuning as these sites change their DOM
- Use `playwright codegen <url>` to quickly capture updated selectors
