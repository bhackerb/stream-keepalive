# Source reliability and discovery

StreamKeeper now learns from actual viewing, separately on each device. Historical
catalog reachability and coverage remain tie breakers, followed by viewers,
English and advertised HD. Playback evidence takes priority when sufficient.

`playback-reliability.json` stores up to 4096 five-minute feed/provider/sport
buckets, never the stream URLs. Evidence has a three-day half-life and expires
after 14 days. Individual feeds need five weighted minutes; provider/sport
fallback needs fifteen. Confidence grows to thirty minutes. Unknown/sparse
sources score 0.5; qualified scores range from 0.05 to 0.95 using the observed
buffering fraction. Rankings use coarse score buckets to limit churn.

Only adjacent fresh samples of the largest primary video count. Media time and
available decoded frames must advance for successful playback. Buffering counts
only after observed progress, after the keeper's existing buffering recognition,
with independent connectivity checks, normal local resource health and no other
simultaneously buffering stream. Pauses, parked windows, startup/autoplay blocking,
missing telemetry, source/frame changes and connectivity failures are inconclusive.
Internet reachability cannot conclusively distinguish every ISP or CDN problem;
penalties are conservative observations, not proof of who caused a stall.

Successful startup time is measured only for opens issued by this daemon and
confirmed by subsequent progress. Uninterrupted observed run length is recorded.
Initial failures are not penalized because they may be browser autoplay policy.
Existing per-stream recovery counts remain visible in status; they are not a
historical provider penalty. No historical evidence is invented or backfilled.
`GET /status` exposes per-provider/sport playback summaries under
`catalog.providers.<provider>.playback`. `GET /player-sources?stream_id=...`
includes a `reliability` explanation for each choice. The source picker, new
catalog-driven starts and automatic failure recovery use the ranking. Healthy
streams and explicit saved/manual source choices are not replaced for a score.

Set `STREAMKEEPER_RELIABILITY_DISABLED=1` in the daemon environment and restart
to suspend gathering and restore catalog/viewer ranking. Each device has its own
evidence; these scores intentionally do not blame every device for one bad Wi-Fi
connection. No signed companion update is needed on the currently deployed builds.

Sampling runs on its own ten-second cadence, separate from catalog network I/O.
Dirty evidence is saved at most once per minute, with a final flush on shutdown;
idle ticks do not rewrite the file. Ranking builds feed/provider totals once per
evidence revision or minute and reuses them for all sources.

## Weekly discovery

The controller runs `streamkeeper-discovery.timer` Mondays at 10:00 America/Denver,
with up to ten minutes of jitter and a catch-up run when the controller returns.
Only the server installs this timer; there is no duplicate laptop scan.

`python3 provider_discovery.py` fetches FMHY's `https://fmhy.net/video#live-sports`,
extracts public HTTPS site origins, and probes at most twenty per run, three at a
time. Oldest/unprobed entries go first. The report `discovery-report.json` includes
HTTP reachability, newly observed sites and unverified candidates. A reachable
homepage is not successful playback. New candidates require manual investigation,
a tested adapter and reviewed code before being admitted to the catalog. This
process does not invoke LLMs, send messages, create board/backlog cards, install
anything, or open streams. FMHY markup failures preserve the previous report
and fail the service visibly. Removed sites disappear from the next report.

Candidate URLs cannot carry credentials, custom ports or private IPs. DNS results
must all be public. Redirects are not followed. Responses, concurrency, request
timeouts, total run time and candidate count are bounded. Only the fixed directory
and extracted public origins are fetched; no arbitrary page scripts are executed.

Install the committed service/timer in `/etc/systemd/system/`, then use
`sudo systemctl enable --now streamkeeper-discovery.timer`. Inspect with
`systemctl list-timers streamkeeper-discovery.timer` and
`journalctl -u streamkeeper-discovery.service`. For an immediate run:
`sudo systemctl start streamkeeper-discovery.service`.

To disable: `sudo systemctl disable --now streamkeeper-discovery.timer`.
Alternatively set `STREAMKEEPER_DISCOVERY_DISABLED=1` in
`/etc/default/streamkeeper-discovery`; the next scheduled run exits without fetching.
