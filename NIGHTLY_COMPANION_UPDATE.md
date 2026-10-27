# Nightly companion update (0.2.11 and onward)

Status as of 2026-10-27: source changes are in the repo, unsigned and not deployed. Signing and installing are manual steps for Ben.

## Tonight's manual fix (already done)

- The OnHockey stream on bhbserv (Flyers vs. Blue Jackets) was stuck at `NO_VIDEO`. The player is a cross-origin `embedsports.me` iframe that the companion never ran in.
- Fix applied: one OS-level click on the embed's play button through XTEST (x=1920, y=312 on the 3840x2160 TV). The stream then reported `PLAYING`. It is still muted, so audio needs unmuting through the normal stream control.

## Autoplay ladder (server-driven, added after tonight's manual click)

Runs once per stream after open (8s grace, then 10s per step). Each step runs only if the previous one did not produce fresh video progress.

1. `play`: `video.play()` in every owned frame (existing).
2. `play_control`: clicks the first player play control in each frame.
3. `pointer`: synthetic pointer and mouse events at the center of the largest video, then `play()`.
4. `os_click`: one trusted X11 click at the center of the largest player iframe (`os_click.py`, XTEST, validated against connected displays).

Outcome per step is written to `autoplay-outcomes.jsonl` beside the state file, with site, step, and `advanced`, `stalled` or `no_telemetry`. The stream's health stats carry `autoplay: {step, state}`; `needs_click` means every step was tried.

Config: `session.autoplay_steps` (list) filters the ladder, for example `['play','os_click']`, to test options per site. Order is always canonical.

Unmute: if the video is embedded and unreachable, unmute clicks only in-frame unmute controls (never mute controls). Audio can't be verified from the page in that case, and the response says so.

Risks to check tonight:
- `play_control`, `pointer` and `os_click` run without telemetry for embeds (cross-origin frames report nothing). If a playing embed gets one of these clicks, it may pause. Watch the first stream closely.
- The new companion ops (`play_control`, `pointer`, `volume_control`, `frame_rects`) need companion 0.2.11. Until it's installed, those steps fail with a ValueError and the ladder moves on.
- The server changes need a `stream-keeper` restart. The restart reattaches owned tabs, but it is not done yet.

Tests: `python3 -m unittest test_autoplay_ladder test_regular_browser test_stream_recovery` (54 pass); `node --test companion/*.test.cjs` (47 pass).

## Source changes in 0.2.11

1. `build_companion.py`: `embedsports.me` added to `HOSTS`, so the content script is injected into the embed frame. Version bumped from 0.2.10 to 0.2.11.
2. `companion/content.js`: the autoplay ladder's frame ops (`play_control`, `pointer`, `volume_control`, `frame_rects`). The earlier one-shot click in the health tick was removed, so the server ladder is the only thing that clicks.
3. `companion/background.js`: routes those ops to owned frames. `frame_rects` goes to the top frame only.
4. Not changed on purpose: `companion/background.js` `HOSTS` and the `ALLOWED_HOSTS` list in `regular_browser.py`. Those gate which top-level URLs the companion will open. The embed is only loaded inside the OnHockey wrapper, so there is no need to widen that allowlist yet.

Tests: `node --test companion/background.test.cjs companion/content.test.cjs` (47 pass). The new click fallback has no dedicated test yet.

## Nightly steps (Ben)

1. Review the diff and run the companion tests.
2. Build: `python3 build_companion.py`. This writes `build/companion/{chrome,firefox}`. The build script generates a throwaway key, so the production signing step is separate and must be done by Ben.
3. Sign and package: Chrome CRX with the production key, Firefox signed package (the `streamkeeper-firefox.zip` artifact).
4. Install on bhbserv (Firefox) and the laptop (ben-laptop) and reload the extension or restart the browser.
5. Confirm `/status` on each device shows `browser_connections` true and the companion capabilities include `onhockey-v1`.
6. Watch one OnHockey game start with no click. Expected health: `PLAYING` within about 30s of open.

## Open items for a permanent fix

- **Autoplay policy may block the fallback.** A script-dispatched `click()` is not a user gesture. Firefox may refuse to start an unmuted video. If the fallback does nothing, use one of:
  - a native-host OS-level click, as done manually tonight (bhbserv is X11; XTEST works), or
  - a Firefox profile preference on the regular browser profile, `media.autoplay.default = 0`. This is a browser setting for Ben to choose, not a companion change.
- **Muted start.** Streams start muted. Unmuting needs a gesture in some setups. Check whether this affects audio tonight.
- **Provider risk.** OnHockey is a wrapper around third-party embeds. Other providers may have the same no-video problem. Watch `health.state` across providers for a few nights before trusting any of them.
- **Plan capacity.** The session plan showed `capacity: 1` while `stream_capacity` is 4. Confirm the intended value before the games start.
- **Laptop.** The laptop checkout is at `f9c7a3e`, pulled tonight. The companion build must go to both devices so their content scripts match.
- **Streamed references.** The current discover path uses the merged catalog. Remaining Streamed mentions are in docs, the `streamed.pk` default in `stream_keeper.py`, and `watch()` help text. Clean these up when convenient.
