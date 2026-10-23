# Laptop streaming investigation — October 22, 2026

## Findings

Evidence was read directly from `ben-laptop` over SSH on October 23. Times below
are laptop local time (America/Denver); the HTTP access records have a conflicting
embedded timezone, so they were not used to establish event times.

- At 18:30:56, `systemd-oomd` selected Firefox for killing. System memory use was
  64,978,296,832 / 67,151,089,664 bytes and swap use was
  60,468,809,728 / 67,150,802,944 bytes. Firefox's cgroup alone had 54.9G swap.
  The journal records selection, not a separate successful-kill receipt. A full
  laptop reboot followed at approximately 18:31. This is evidence of severe
  memory exhaustion, not proof of which page/extension/component caused it.
- The post-reboot Firefox session ended at 22:14:54 with an 8G memory peak over
  3h42m. The StreamKeeper daemon peaked at 111.5M. The earlier runaway and the
  later multi-stream lag must not be assumed to have the same cause.
- 19:23:22 and 20:41:57 session errors were connection timeouts in aiohttp while
  fetching Streamed's catalog API. They were not Firefox command timeouts.
- Hardware: i7-10750H (6 cores / 12 threads), 64 GB installed RAM, Intel UHD
  integrated GPU and NVIDIA GTX 1650 Ti. Swap is compressed zram, not disk swap.
  `vainfo` successfully reports Intel iHD H.264 decode support. Saved Firefox
  telemetry reports Intel WebRender/hardware compositing, but does not establish
  actual hardware video decoding for yesterday's players.
- A 3-second synthetic 1920x1080, 60fps H.264 clip decoded successfully on the
  laptop using mpv and Intel VA-API. Its log explicitly reports
  `Using hardware decoding (vaapi-copy)`. This validates a native decode path,
  not a six- or ten-stream throughput guarantee or browser decode verification.
- A second short probe ran six of those clips simultaneously: all six exited
  successfully and logged hardware decode. Elapsed time was 3.22 seconds,
  aggregate process RSS peaked at 696.7 MiB, and aggregate CPU averaged 262.5%
  of one core (about 2.6 cores). These were local, headless players; no network,
  browser scripts, display compositor, or long-duration growth was exercised.

## Memory guards and diagnostics

Regular mode now defaults to `streams.max_streams` (10 if unspecified), rather
than silently using at least 20. Explicit `session.max_open_streams` overrides
remain supported, including parked windows. The laptop's existing explicit
configuration should be inspected rather than overwritten.

`/status` includes `resources`, `stream_capacity`, and `memory_guard`.
Linux resource samples are collected off the event loop every 10 seconds,
cached for at most 5 seconds when opening windows, and logged once per minute
in the existing rotating service log. They contain available RAM, used swap,
memory PSI, browser process RSS/swap, CPU deltas, and five largest processes per
browser. They contain no page URLs, titles, or process command lines. RSS sums
include shared pages; CPU is a percentage of one core; PID start times prevent
PID reuse from creating false CPU spikes.

With the default `session.memory_guard: true`, opening windows is deferred when:

- Available RAM falls below the larger of 2 GiB or 10% of total RAM.
- Memory PSI over 10 seconds reaches 10% full stalls or 25% partial stalls.
- Either browser's RSS plus swap exceeds the larger of 4 GiB or half of RAM.
- The memory sample is unavailable.

Existing windows are never automatically killed for memory use. Automatic
reconciliation/recovery and local keepalive defer mutations under pressure;
explicit Stop still works. This limits further load but cannot free memory
already retained by a page. Use Firefox `about:processes` to associate the logged
PIDs with pages and `about:memory` to investigate growth. Compare a fresh memory
snapshot as each game is added. Hardware decode must be checked in Firefox's
`about:support` during a real playing session; avoid blindly forcing graphics
preferences or moving everything to the NVIDIA GPU.

## Companion 0.2.6

The companion adds decoded/dropped frame counts, buffer-ahead seconds, playback
rate, and distance to the seekable live edge. That last measurement is not
end-to-end broadcast latency, and is null for finite-duration VOD. Metrics are
selected from the largest fresh video, not a small playing advertisement. Older
companions continue working and report missing metrics as null.

Only one health request per frame can be outstanding, preventing periodic polls
from accumulating behind a slow native response. Audio DOM scans are coalesced
over 200ms on owned pages. Existing ownership and audio controls remain in force.

Build with `python3 build_companion.py`. Upload `build/streamkeeper-firefox.zip`
for Mozilla **unlisted signing**, then install the signed XPI using the existing
`install_signed_companion.sh` workflow. Do not install the unsigned ZIP as a
permanent extension or disable signature checks. A Firefox restart is needed to
activate the policy-installed update; leave current playback alone until then.

## Sources outside Streamed

OnHockey's current site was inspected in a browser after direct HTTP requests
returned 403. Listings link to `https://www.onhockey.tv/np_stream400.php?...`
wrappers targeting its player frame. The wrapper is now an allowlisted source
in regular mode and companion 0.2.6. Open a specific wrapper using authenticated
`POST /watch` with its `url` and `label`; existing Stop, layout and player health
operate on the resulting owned window. Opening OnHockey is blocked with a clear
upgrade message until a companion advertises `onhockey-v1`.

This is manual source support, not an automatic OnHockey catalog/scheduler.
External links that navigate the top-level tab away from OnHockey are not
automatically trusted. Some OnHockey links use embed hosts also used by Streamed;
listing a second site does not prove an independent upstream. Inspect the actual
provider and compare playback before treating it as redundancy. Live playback
through the signed 0.2.6 companion remains to be validated after signing.

For a provider supplying a direct HTTPS HLS/DASH/video URL:

```bash
python3 native_player.py 'https://provider.example/live.m3u8'
```

The helper invokes mpv without a shell, config, external scripts, or webpage
extractors, requests automatic hardware decoding, starts muted, and limits
forward/back demux buffers to 64/8 MiB with a 10-second cache target. Use
`--unmute` for sound. It needs a direct media URL, not an embed or game listing;
DRM and browser-authenticated sources are not supported. It is a standalone
player, not part of the automated session layout or browser window count.

## Validation

Run `python3 -m unittest discover -q`, `node --test test_companion_metrics.cjs`,
and `python3 build_companion.py`. A real multi-game session is still required
to establish sustainable stream count and identify the original memory runaway.

October 23 validation: 187 Python tests and five Node behavior tests passed.
The staged laptop suite passed before activation; the final parser regression
also passed on the laptop. Packaged manifest version/OnHockey matches were
verified. Independent Grok reviews passed for the companion/security lens and
the memory-guard/resource/correctness lens, with no material findings. Claude
was unavailable in the configured reviewer catalog. The first larger resource
review timed out and the local fallback produced no valid verdict; a smaller
implementation-only second round passed. These reviews do not substitute for
the outstanding signed-extension and sustained live-stream validation.
