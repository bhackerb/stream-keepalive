"""Advisory, device-local playback evidence. Never controls or opens a player.

Only adjacent fresh primary-video observations count. Pauses, gaps, source changes,
startup blocking, and uncorroborated stalls are excluded. Five-minute buckets keep
storage bounded; evidence decays with a three-day half-life and expires at 14 days.
"""
import hashlib
import json
import math
import os
import time
from pathlib import Path


class PlaybackReliability:
    def __init__(self, path, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.rows, self.observed = {}, {}
        self.pending_starts = {}
        self.runs = {}
        self.last_save = 0
        self.dirty = False
        self._index = None
        self._index_at = 0
        try:
            saved = json.loads(self.path.read_text())
            if saved.get('version') == 1:
                for key, row in saved.get('rows', {}).items():
                    if (isinstance(key, str) and isinstance(row, dict) and
                            all(type(row.get(k)) in (int, float) and math.isfinite(row[k]) and row[k] >= 0
                                for k in ('at', 'playing', 'buffering')) and
                            all(type(row.get(k, 0)) in (int, float) and math.isfinite(row.get(k, 0)) and row.get(k, 0) >= 0
                                for k in ('startup_successes', 'startup_seconds', 'longest_run_seconds')) and
                            all(isinstance(row.get(k), str) for k in ('provider', 'sport', 'feed'))):
                        self.rows[key] = row
        except (OSError, ValueError, AttributeError, TypeError):
            pass
        self._prune()
        self.dirty = False

    @staticmethod
    def feed(url):
        return hashlib.sha256(url.encode()).hexdigest()[:24]

    def opened(self, sid, url):
        self.pending_starts[sid] = (url, self.clock())
        self.observed.pop(sid, None)
        self.runs.pop(sid, None)

    def _prune(self):
        cutoff = self.clock() - 14 * 86400
        kept = dict(sorted(((k, r) for k, r in self.rows.items() if cutoff <= r['at'] <= self.clock() + 60),
                           key=lambda pair: pair[1]['at'])[-4096:])
        if len(kept) != len(self.rows):
            self.dirty = True
            self._index = None
        self.rows = kept

    def observe(self, sid, url, provider, sport, frame, health, at, *, network_ok=False):
        previous = self.observed.pop(sid, None)
        if (not provider or not sport or health.get('paused') is True or health.get('ended') is True or
                health.get('state') not in ('PLAYING', 'BUFFERING')):
            self.runs.pop(sid, None)
            return
        current = (url, provider, sport, frame, at, dict(health), False)
        self.observed[sid] = current
        if not previous or previous[:4] != current[:4]:
            self.runs.pop(sid, None)
            return
        elapsed = at - previous[4]
        if not 0 < elapsed <= 15:
            self.runs.pop(sid, None)
            return
        before = previous[5]
        times = (health.get('current_time'), before.get('current_time'))
        advanced = all(type(v) in (int, float) and math.isfinite(v) for v in times) and times[0] > times[1]
        a, b = before.get('decoded_frames'), health.get('decoded_frames')
        advanced = advanced and (a is None or b is None or
                                all(type(v) in (int, float) and math.isfinite(v) for v in (a, b)) and b > a)
        playing = elapsed if health['state'] == before['state'] == 'PLAYING' and advanced else 0
        if not playing:
            self.runs.pop(sid, None)
        proven = bool(playing or previous[6])
        self.observed[sid] = (*current[:6], proven)
        # Buffering is already normalized by the keeper after proven progress.
        buffering = elapsed if proven and health['state'] == before['state'] == 'BUFFERING' and network_ok else 0
        if not playing and not buffering:
            return
        feed = self.feed(url)
        key = f'{provider}|{sport}|{feed}|{int(at // 300)}'
        row = self.rows.setdefault(key, dict(provider=provider, sport=sport, feed=feed, at=at, playing=0, buffering=0))
        row['at'] = at
        row['playing'] += playing
        row['buffering'] += buffering
        self.dirty = True
        self._index = None
        run = self.runs.get(sid, (url, 0))
        uninterrupted = (run[1] if run[0] == url else 0) + playing if playing else 0
        self.runs[sid] = (url, uninterrupted)
        row['longest_run_seconds'] = max(row.get('longest_run_seconds', 0), uninterrupted)
        if playing and before['state'] == 'PLAYING':
            pending = self.pending_starts.pop(sid, None)
            if pending and pending[0] == url and 0 <= at - pending[1] <= 600:
                row['startup_successes'] = row.get('startup_successes', 0) + 1
                row['startup_seconds'] = row.get('startup_seconds', 0) + at - pending[1]
        self._prune()
        if self.clock() - self.last_save >= 60:
            self.save()

    def forget(self, active_ids):
        self.observed = {k: v for k, v in self.observed.items() if k in active_ids}
        self.pending_starts = {k: v for k, v in self.pending_starts.items() if k in active_ids}
        self.runs = {k: v for k, v in self.runs.items() if k in active_ids}

    def evidence(self, provider, sport, url=None):
        now = self.clock()
        feed = self.feed(url) if url else None
        # One pass builds both feed and provider/sport totals. Every source in
        # a ranking pass then performs constant-time lookups, not a history scan.
        if self._index is None or not 0 <= now - self._index_at < 60:
            self._index = {}
            for row in self.rows.values():
                if not 0 <= now - row['at'] <= 14 * 86400:
                    continue
                weight = 2 ** (-(now - row['at']) / (3 * 86400))
                for scope in (None, row['feed']):
                    key = (row['provider'], row['sport'], scope)
                    totals = self._index.setdefault(key, [0, 0, 0, 0, 0])
                    totals[0] += row['playing'] * weight
                    totals[1] += row['buffering'] * weight
                    totals[2] += row.get('startup_successes', 0)
                    totals[3] += row.get('startup_seconds', 0)
                    totals[4] = max(totals[4], row.get('longest_run_seconds', 0))
            self._index_at = now
        playing, buffering, starts, startup_seconds, longest_run = self._index.get((provider, sport, feed), (0, 0, 0, 0, 0))
        seconds = playing + buffering
        minimum = 300 if url else 900
        confidence = min(1, seconds / 1800) if seconds >= minimum else 0
        ratio = buffering / seconds if seconds else 0
        return dict(score=round(.5 + .45 * confidence * (1 - 2 * ratio), 3),
                    observed_seconds=round(seconds), buffering_ratio=round(ratio, 4),
                    confidence=round(confidence, 3), scope='feed' if url else 'provider', sport=sport,
                    startup_successes=starts,
                    mean_startup_seconds=round(startup_seconds/starts, 1) if starts else None,
                    longest_run_seconds=round(longest_run))

    def rating(self, provider, sport, url):
        if os.environ.get('STREAMKEEPER_RELIABILITY_DISABLED') == '1':
            return dict(score=.5, confidence=0, scope='disabled', sport=sport)
        feed = self.evidence(provider, sport, url)
        return feed if feed['confidence'] else self.evidence(provider, sport)

    def save(self, *, force=False):
        if not self.dirty or not force and self.clock() - self.last_save < 60:
            return
        self._prune()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix('.tmp')
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(dict(version=1, rows=self.rows), handle, allow_nan=False)
            temp.replace(self.path)
            self.last_save = self.clock()
            self.dirty = False
        except OSError:
            pass  # Advisory evidence must never stop playback.
