"""Multi-provider live-sports catalog.

Every adapter turns one site's public schedule JSON into normalized events. Events from all
reachable providers merge into planner-shaped matches, so no site is primary: one provider
failing only drops its sources. Per-provider health accumulates the consistency evidence
(reachability, live coverage) that ranks playback choices and shows which sites hold up.
"""
import asyncio
import html
import json
import math
import os
import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import aiohttp

ET = ZoneInfo('America/New_York')
USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0'
HEALTH_WINDOW = 288  # polls kept per provider (~24h of 5-minute background polls)
ALIAS_TTL_MS = 48 * 3600_000  # game ids must outlive a session so restarts never duplicate a window
STALE_EVENT_MS = 8 * 3600_000  # 24/7 channels carry months-old start times; skip them
MATCH_WINDOW_MS = 3 * 3600_000
FINISHED_STATUSES = {'FT', 'AET', 'PEN', 'FIN', 'FINAL', 'ENDED', 'CANC', 'CANCELLED', 'PST', 'POSTP', 'ABD', 'AWD', 'WO'}


class CatalogUnavailable(ValueError):
    """Upstream stream catalog could not be read; existing streams are left untouched."""


# Ordered: specific labels before the generic 'football'. Provider labels only, never titles of user text.
CATEGORY_LABELS = (
    ('other', r'australian football|\bafl\b|aussie rules'),
    ('american-football', r'american football|\bnfl\b|ncaaf|college football|redzone'),
    ('hockey', r'hockey|\bnhl\b|\bkhl\b|\bahl\b'),
    ('basketball', r'basketball|\bnba\b|\bwnba\b|ncaab|ncaaw|euroleague'),
    ('baseball', r'baseball|\bmlb\b|\bnpb\b|\bkbo\b'),
    ('motor-sports', r'motor|racing|formula|\bf1\b|nascar|indycar|motogp|dirtvision|rally'),
    ('wrestling', r'wrestling|\bwwe\b|\baew\b|\btna\b|\bnxt\b'),
    ('fight', r'\bmma\b|\bufc\b|boxing|bare knuckle|martial|combat|\bfight|\bbkfc\b|\bpfl\b'),
    ('rugby', r'rugby'),
    ('tennis', r'tennis'),
    ('cricket', r'cricket|\bipl\b'),
    ('football', r'soccer|football|futsal|premier league|la liga|serie a|bundesliga|ligue|\bmls\b|uefa'),
)


def category_of(*labels):
    text = ' '.join(str(label) for label in labels if label).lower()
    for category, pattern in CATEGORY_LABELS:
        if re.search(pattern, text):
            return category
    return 'other'


def words(text):
    text = unicodedata.normalize('NFKD', str(text or '')).encode('ascii', 'ignore').decode().lower()
    return re.findall(r'[a-z0-9]+', text)


SEPARATORS = re.compile(r'\s+(?:vs\.?|v\.?|@|at|-|–|—)\s+', re.I)
GENERIC = {'fc', 'cf', 'sc', 'afc', 'ac', 'cd', 'the', 'club', 'de', 'city', 'united', 'real', 'sporting',
           'athletic', 'st', 'saint', 'new', 'state', 'university', 'of', 'and'}


def split_teams(title):
    parts = [p.strip() for p in SEPARATORS.split(str(title or '')) if p.strip()]
    return parts if len(parts) == 2 else []


def team_tokens(name):
    return {w for w in words(name) if len(w) >= 3 and w not in GENERIC}


def stream_rank(item):
    # Most-watched source first; English and then HD only break ties. Unknown counts rank as zero.
    viewers = item.get('viewers') if type(item.get('viewers')) in (int, float) and math.isfinite(item['viewers']) else 0
    language = str(item.get('language') or '').strip().lower()
    english = language == 'english' or language.startswith('english') and not language[7:8].isalpha()
    return (viewers, english, bool(item.get('hd')))


def _int(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if math.isfinite(number) and number >= 0 else None


def _https(url):
    return isinstance(url, str) and url.startswith('https://') and len(url) <= 2048 and not re.search(r'[\s\\]', url)


def _event(provider, pid, title, category, start_ms, streams, *, teams=None, end_ms=None, live=None, viewers=None):
    streams = [s for s in streams if _https(s.get('url'))]
    if not isinstance(start_ms, int) or start_ms <= 0 or not title or not streams:
        return None
    return {'provider': provider, 'pid': str(pid), 'title': str(title).strip()[:200], 'category': category,
            'teams': teams if teams is not None else split_teams(title), 'start_ms': start_ms, 'end_ms': end_ms,
            'live': live, 'viewers': viewers, 'streams': streams}


def _stream(url, label, *, language=None, hd=False, viewers=None):
    return {'url': url, 'label': str(label or 'Stream')[:80], 'language': language, 'hd': bool(hd), 'viewers': viewers}


# --------------------------------------------------------------------------- adapters

def parse_ppv(payload, now_ms):
    events = []
    for group in payload.get('streams') or []:
        for item in (group.get('streams') or []) if isinstance(group, dict) else []:
            if not isinstance(item, dict) or item.get('always_live'):
                continue
            start, end = _int(item.get('starts_at')), _int(item.get('ends_at'))
            if not start:
                continue
            viewers = _int(item.get('viewers'))
            language = 'English' if item.get('locale') in (None, 'en') else item.get('locale')
            streams = [_stream(item.get('iframe'), item.get('source_tag') or 'PPV', language=language, viewers=viewers)]
            for sub in item.get('substreams') or []:
                if isinstance(sub, dict):
                    sub_language = 'English' if sub.get('locale') in (None, 'en') else sub.get('locale')
                    streams.append(_stream(sub.get('iframe'), sub.get('source_tag') or 'PPV alt', language=sub_language))
            start_ms, end_ms = start * 1000, (end * 1000 if end else None)
            events.append(_event('ppv', item.get('id'), item.get('name'), category_of(group.get('category'), item.get('category_name'), item.get('tag')),
                                 start_ms, streams, end_ms=end_ms, viewers=viewers,
                                 live=start_ms <= now_ms < end_ms if end_ms else None))
    return events


def parse_streamfree(payload, now_ms):
    events = []
    for item in payload.get('streams') or []:
        if not isinstance(item, dict):
            continue
        start = _int(item.get('match_timestamp'))
        if not start or now_ms - start * 1000 > STALE_EVENT_MS:
            continue
        # StreamFree files the NFL as 'football' and soccer as 'soccer'.
        label = 'american football' if item.get('category') == 'football' else item.get('category')
        teams = [t.get('name') for t in (item.get('team1'), item.get('team2')) if isinstance(t, dict) and t.get('name')]
        viewers = _int(item.get('viewers'))
        streams = [_stream(url, f'StreamFree {i + 1}', viewers=viewers, hd='1080' in url) for i, url in enumerate(item.get('sources') or [])]
        events.append(_event('streamfree', item.get('id'), item.get('name'), category_of(label, item.get('league')), start * 1000, streams,
                             teams=teams if len(teams) == 2 else None, viewers=viewers, live=True if start * 1000 <= now_ms else None))
    return events


def parse_timstreams(payload, now_ms):
    genres = {g.get('id'): g.get('name') for g in payload.get('genres') or [] if isinstance(g, dict)}
    events = []
    for item in payload.get('events') or []:
        if not isinstance(item, dict) or item.get('vip'):
            continue
        try:  # TimStreams publishes Eastern wall-clock times without an offset.
            start_ms = int(datetime.fromisoformat(item['time']).replace(tzinfo=ET).timestamp() * 1000)
        except (KeyError, TypeError, ValueError):
            continue
        viewers = _int(item.get('viewers'))
        streams = [_stream(s.get('url'), s.get('name'), language='English') for s in item.get('streams') or [] if isinstance(s, dict) and not s.get('vip')]
        events.append(_event('timstreams', item.get('url'), item.get('name'), category_of(genres.get(item.get('genre'))), start_ms, streams,
                             viewers=viewers or None))
    return events


def parse_rockylive(payload, now_ms):
    events = []
    # The current public API advertises event pages rather than individual embeds.
    for item in payload.get('events') or []:
        if not isinstance(item, dict):
            continue
        start = _int(item.get('start_timestamp'))
        if (not start or now_ms - start * 1000 > STALE_EVENT_MS or item.get('finished') is True or
                item.get('dead') is True or str(item.get('status') or '').upper() in FINISHED_STATUSES or
                not _https(item.get('event_url'))):
            continue
        events.append(_event('rockylive', item.get('id'), item.get('title'),
                             category_of(item.get('league'), item.get('league_slug')), start * 1000,
                             [_stream(item.get('event_url'), 'Rockylive player')]))
    for item in payload.get('matches') or []:
        if not isinstance(item, dict):
            continue
        start = _int(item.get('ts_et'))  # despite the name, a true UTC epoch
        status = str(item.get('status') or '').upper()
        if not start or status in FINISHED_STATUSES:
            continue
        streams = [_stream(s.get('link'), f"Rocky HD {s.get('hd')}", hd=True) for s in item.get('streams') or [] if isinstance(s, dict)]
        events.append(_event('rockylive', item.get('id'), item.get('title'), category_of(item.get('category'), item.get('league')), start * 1000, streams,
                             live=status == 'LIVE' or None))
    return events


def parse_matchora(payload, now_ms):
    """Keep match-specific channel choices; never claim listing health is playback proof."""
    events = []
    for item in payload.get('events') or []:
        if not isinstance(item, dict):
            continue
        start = _int(item.get('kickoff'))
        if (not start or now_ms - start * 1000 > STALE_EVENT_MS or
                item.get('finished') is True or str(item.get('status') or '').upper() in FINISHED_STATUSES):
            continue
        home, away = item.get('home'), item.get('away')
        if not isinstance(home, str) or not home.strip() or not isinstance(away, str) or not away.strip():
            continue
        streams = []
        seen = set()
        for channel in item.get('channels') or []:
            if not isinstance(channel, dict) or channel.get('dead') is True:
                continue
            url = channel.get('embed_url')
            if not _https(url) or url in seen:
                continue
            seen.add(url)
            quality = str(channel.get('quality') or '').upper()
            streams.append(_stream(url, channel.get('name') or 'Matchora channel',
                                   language=channel.get('lang'), hd=quality in ('HD', 'FHD', 'UHD', '4K', '1080P', '720P'),
                                   viewers=_int(channel.get('watching'))))
        # Some events expose only the aggregate player, without channel details.
        if (not streams and not item.get('channels') and _int(item.get('channel_count')) and
                _https(item.get('embed_url'))):
            streams.append(_stream(item.get('embed_url'), 'Matchora player'))
        events.append(_event('matchora', item.get('id'), f'{away} @ {home}',
                             category_of(item.get('sport'), item.get('league')), start * 1000, streams,
                             teams=[home, away], live=item.get('live') if type(item.get('live')) is bool else None,
                             viewers=_int(item.get('watching'))))
    return events


def parse_cdnlivetv(payload, now_ms):
    events = []
    sports = payload.get('cdn-live-tv') or {}
    for sport, items in sports.items() if isinstance(sports, dict) else []:
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            status = str(item.get('status') or '').upper()
            if status in FINISHED_STATUSES:
                continue
            try:
                start_ms = int(datetime.strptime(item['start'], '%Y-%m-%d %H:%M').replace(tzinfo=timezone.utc).timestamp() * 1000)
                end_ms = int(datetime.strptime(item['end'], '%Y-%m-%d %H:%M').replace(tzinfo=timezone.utc).timestamp() * 1000) if item.get('end') else None
            except (KeyError, TypeError, ValueError):
                continue
            streams = [_stream(c.get('url'), c.get('channel_name'), viewers=_int(c.get('viewers'))) for c in item.get('channels') or [] if isinstance(c, dict)]
            teams = [item.get('homeTeam'), item.get('awayTeam')]
            viewers = max([s['viewers'] for s in streams if s['viewers']] or [0]) or None
            events.append(_event('cdnlivetv', item.get('gameID'), item.get('event'), category_of(sport, item.get('tournament')), start_ms, streams,
                                 teams=teams if all(teams) else None, end_ms=end_ms, viewers=viewers,
                                 live=False if status == 'NS' else True if status else None))
    return events


ONHOCKEY_LEAGUE = re.compile(r"<tbody class='[^']*'>.*?<b>([^<]+)</b>(.*?)(?=<tbody class=|$)", re.S)
ONHOCKEY_GAME = re.compile(r"<tr class='game'[^>]*><td><text class='game_hour'[^>]*>(\d{1,2})</text>:(\d{2})</td><td>([^<]*)<div class='gamelinks'[^>]*>(.*?)</div>", re.S)
ONHOCKEY_LINK = re.compile(r"<a href='(np_stream400\.php\?channel=//[^'\s]+)'[^>]*?title='([^']*)'[^>]*>([^<]*)</a>")


def parse_onhockey(page, now_ms):
    """OnHockey's board is a rolling ~24h window of UTC hours with no dates."""
    now = datetime.fromtimestamp(now_ms / 1000, timezone.utc)
    events = []
    for league, body in ONHOCKEY_LEAGUE.findall(page):
        for hour, minute, title, links in ONHOCKEY_GAME.findall(body):
            start = now.replace(hour=int(hour) % 24, minute=int(minute) % 60, second=0, microsecond=0)
            if start < now - timedelta(hours=6):
                start += timedelta(days=1)
            elif start > now + timedelta(hours=20):
                start -= timedelta(days=1)
            streams = []
            for segment in re.split(r'<br>', links):
                language = re.match(r'\s*([a-z]+):', segment)
                for href, channel, name in ONHOCKEY_LINK.findall(segment):
                    label = ' · '.join(x for x in (html.unescape(channel).strip(), html.unescape(name).strip()) if x)
                    streams.append(_stream('https://www.onhockey.tv/' + html.unescape(href), label or 'OnHockey',
                                           language=language.group(1).capitalize() if language else None))
            name = html.unescape(title).strip()
            start_ms = int(start.timestamp() * 1000)
            events.append(_event('onhockey', f'{league}|{name}|{hour}:{minute}', name, 'hockey', start_ms, streams, end_ms=start_ms + 12600_000))
    return events


@dataclass(frozen=True)
class Provider:
    name: str
    label: str
    urls: tuple  # mirrors, tried in order
    parse: Callable
    kind: str = 'json'
    headers: tuple = ()  # extra request headers the site's own page sends
    sports: tuple = ()  # specialists are scored only against live games in their sports


PROVIDERS = (
    Provider('ppv', 'PPV.ST', ('https://api.ppv.st/api/streams', 'https://api.ppv.cx/api/streams'), parse_ppv),
    Provider('streamfree', 'StreamFree', ('https://strmfree.st/api/v1/streams', 'https://streamfree.top/api/v1/streams'), parse_streamfree),
    Provider('timstreams', 'TimStreams', ('https://timst.top/api/live-upcoming', 'https://tim-streams.com/api/live-upcoming'), parse_timstreams),
    Provider('rockylive', 'Rockylive', ('https://rockylive.st/api/events.php?limit=1000', 'https://rockylive.st/api-event.php'), parse_rockylive),
    Provider('cdnlivetv', 'CDN Live TV', ('https://api.cdnlivetv.is/api/v1/events/sports/',), parse_cdnlivetv),
    Provider('onhockey', 'OnHockey', ('https://www.onhockey.tv/schedule_table.php',), parse_onhockey, kind='html',
             headers=(('Referer', 'https://www.onhockey.tv/'),), sports=('hockey',)),
    Provider('matchora', 'Matchora', ('https://matchora.to/api/v1/schedule?watchable=1',), parse_matchora),
)


# --------------------------------------------------------------------------- merge

def same_game(a, b):
    if a['category'] != b['category'] and 'other' not in (a['category'], b['category']):
        return False
    if abs(a['start_ms'] - b['start_ms']) > MATCH_WINDOW_MS:
        return False
    if len(a['teams']) == 2 and len(b['teams']) == 2:
        (a1, a2), (b1, b2) = map(team_tokens, a['teams']), map(team_tokens, b['teams'])
        return bool((a1 & b1 and a2 & b2) or (a1 & b2 and a2 & b1))
    left, right = set(words(a['title'])), set(words(b['title']))
    return abs(a['start_ms'] - b['start_ms']) <= 5400_000 and bool(left | right) and len(left & right) / len(left | right) >= .5


def naming_rank(event):
    # Fullest team names name the game, so a short-form listing ("Detroit") never renames it.
    return sum(len(team_tokens(t)) for t in event['teams']) if len(event['teams']) == 2 else 0


def event_key(event):
    date = datetime.fromtimestamp(event['start_ms'] / 1000, ET).strftime('%Y-%m-%d')
    names = [words(t)[-1] for t in event['teams'] if words(t)] if len(event['teams']) == 2 else []
    slug = '-'.join(sorted(names)) if len(names) == 2 else '-'.join(words(event['title'])[:5])
    return f'{date}-{slug or event["provider"]}'[:96]


# --------------------------------------------------------------------------- health

class ProviderHealth:
    """Rolling per-provider poll evidence persisted beside the session state."""

    def __init__(self, path):
        self.path = Path(path)
        self.polls = {}
        self.aliases = {}  # 'provider|pid' -> [match id, last seen ms]
        try:
            saved = json.loads(self.path.read_text())
            self.polls = {k: v[-HEALTH_WINDOW:] for k, v in saved.get('polls', {}).items() if isinstance(v, list)}
            self.aliases = {k: v for k, v in saved.get('aliases', {}).items()
                            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and isinstance(v[1], int)}
        except (OSError, ValueError, AttributeError):
            pass

    def record(self, name, row):
        self.polls.setdefault(name, []).append(row)
        del self.polls[name][:-HEALTH_WINDOW]

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix('.tmp')
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as f:
                f.write(json.dumps({'polls': self.polls, 'aliases': self.aliases}))
            temp.replace(self.path)
        except OSError:
            pass  # evidence is advisory; never block playback on it

    def score(self, name):
        rows = self.polls.get(name, [])
        if len(rows) < 3:
            return .5  # no evidence yet: rank on viewers alone
        ok = [r for r in rows if r.get('ok')]
        coverage = [r['coverage'] for r in ok if isinstance(r.get('coverage'), (int, float))]
        return round(len(ok) / len(rows) * (.4 + .6 * (sum(coverage) / len(coverage) if coverage else 0)), 3)

    def summary(self, providers):
        out = {}
        for p in providers:
            rows = self.polls.get(p.name, [])
            ok = [r for r in rows if r.get('ok')]
            last = rows[-1] if rows else {}
            out[p.name] = {
                'label': p.label, 'polls': len(rows), 'ok_rate': round(len(ok) / len(rows), 3) if rows else None,
                'score': self.score(p.name), 'last_at': last.get('at'), 'last_ok': last.get('ok'), 'last_error': last.get('error'),
                'mirror': last.get('mirror'), 'latency_ms': last.get('latency_ms'), 'events': last.get('events'),
                'live': last.get('live'), 'coverage': last.get('coverage'),
                'avg_coverage': round(sum(r.get('coverage') or 0 for r in ok) / len(ok), 3) if ok else None,
            }
        return out


# --------------------------------------------------------------------------- catalog

class Catalog:
    def __init__(self, health_path, providers=PROVIDERS, disabled=()):
        self.providers = tuple(p for p in providers if p.name not in set(disabled))
        self.health = ProviderHealth(health_path)
        self.last = None
        self.last_at = 0
        self.urls = set()
        self._previous_urls = set()

    async def _fetch_provider(self, client, provider, now_ms):
        error = None
        for url in provider.urls:
            started = time.monotonic()
            try:
                headers = {'User-Agent': USER_AGENT, 'Accept': 'application/json' if provider.kind == 'json' else 'text/html', **dict(provider.headers)}
                async with client.get(url, headers=headers) as res:
                    res.raise_for_status()
                    if provider.kind not in (res.content_type or ''):
                        raise ValueError('returned ' + (res.content_type or 'no content type'))
                    payload = await res.json() if provider.kind == 'json' else await res.text()
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
                error = f'{url}: {type(e).__name__} {str(e)[:120]}'.strip()
                continue
            latency = int((time.monotonic() - started) * 1000)
            try:  # a changed schema is a provider failure, not a catalog crash
                events = [e for e in provider.parse(payload, now_ms) if e] if isinstance(payload, dict if provider.kind == 'json' else str) else None
            except (KeyError, TypeError, ValueError, AttributeError) as e:
                events, error = None, f'{url}: schema changed ({type(e).__name__})'
            if events is None:
                error = error or f'{url}: unexpected payload'
                continue
            return {'ok': True, 'mirror': url, 'latency_ms': latency}, events
        return {'ok': False, 'error': error}, []

    async def fetch(self, client, now_ms=None):
        if client is None:
            raise CatalogUnavailable('Stream catalog client is not started')
        now_ms = now_ms or int(time.time() * 1000)
        results = await asyncio.gather(*(self._fetch_provider(client, p, now_ms) for p in self.providers))
        events, polls = [], {}
        for provider, (poll, provider_events) in zip(self.providers, results):
            fresh = [e for e in provider_events if not e['end_ms'] or e['end_ms'] > now_ms - 900_000]
            polls[provider.name] = dict(poll, at=int(now_ms / 1000), events=len(fresh))
            events.extend(fresh)
        if not any(p['ok'] for p in polls.values()):
            for name, poll in polls.items():
                self.health.record(name, poll)
            self.health.save()
            raise CatalogUnavailable('No stream catalog reachable: ' + '; '.join(f'{k} {v["error"]}' for k, v in polls.items())[:400])
        matches = self._merge(events, now_ms)
        for provider in self.providers:
            poll = polls[provider.name]
            if poll['ok']:
                live = [m for m in matches if m['live'] and (not provider.sports or m['category'] in provider.sports)]
                poll['live'] = sum(1 for m in live if provider.name in m['providers'])
                poll['coverage'] = round(poll['live'] / len(live), 3) if live else None
            self.health.record(provider.name, poll)
        self.health.save()
        for match in matches:
            match['sources'] = self.rank(match['sources'])
        self._previous_urls, self.urls = self.urls, {s['url'] for m in matches for s in m['sources']}
        self.last, self.last_at = {'matches': matches, 'providers': self.health.summary(self.providers), 'fetched_at': now_ms}, time.time()
        return self.last

    def known_url(self, url):
        return url in self.urls or url in self._previous_urls

    def rank(self, sources):
        # Consistent providers first (coarse buckets so noise cannot reshuffle), then viewers, English, HD.
        return sorted(sources, key=lambda s: (round(self.health.score(s['provider']), 1), *stream_rank(s)), reverse=True)

    def _merge(self, events, now_ms):
        order = {p.name: i for i, p in enumerate(self.providers)}
        clusters = []
        for event in sorted(events, key=lambda e: (order.get(e['provider'], 99), e['start_ms'])):
            for cluster in clusters:
                if any(same_game(event, other) for other in cluster):
                    cluster.append(event)
                    break
            else:
                clusters.append([event])
        matches, used = [], set()
        aliases = self.health.aliases
        for cluster in clusters:
            primary = cluster[0]
            best = max(cluster, key=lambda e: (naming_rank(e), -order.get(e['provider'], 99)))
            known = Counter(aliases[f"{e['provider']}|{e['pid']}"][0] for e in cluster if f"{e['provider']}|{e['pid']}" in aliases)
            mid = next((m for m, _ in known.most_common() if m not in used), None)
            if not mid:
                base = event_key(best)
                mid, suffix = base, 2
                while mid in used:
                    mid, suffix = f'{base}-{suffix}', suffix + 1
            used.add(mid)
            for e in cluster:
                aliases[f"{e['provider']}|{e['pid']}"] = [mid, now_ms]
            start = min(e['start_ms'] for e in cluster)
            ends = [e['end_ms'] for e in cluster if e['end_ms']]
            declared = [e['live'] for e in cluster if e['live'] is not None]
            live = any(declared) if declared else start <= now_ms and (not ends or now_ms < max(ends))
            teams = best['teams'] if len(best['teams']) == 2 else []
            viewers = [e['viewers'] for e in cluster if e['viewers'] is not None]
            sources, seen = [], set()
            for e in cluster:
                for s in e['streams']:
                    if s['url'] not in seen:
                        seen.add(s['url'])
                        sources.append(dict(s, provider=e['provider']))
            matches.append({
                'id': mid, 'title': best['title'], 'category': primary['category'], 'date': start, 'live': live,
                'teams': {'home': {'name': teams[0]}, 'away': {'name': teams[1]}} if teams else {},
                'viewers': max(viewers) if viewers else None, 'providers': sorted({e['provider'] for e in cluster}),
                'sources': sources,
            })
        for key in [k for k, (_, seen_ms) in aliases.items() if now_ms - seen_ms > ALIAS_TTL_MS]:
            del aliases[key]
        return matches


async def _probe(rounds, interval):
    """Manual consistency check: python3 catalog_providers.py [rounds] [interval_seconds]."""
    catalog = Catalog(Path(os.environ.get('STREAMKEEPER_HEALTH', 'provider-health.json')))
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as client:
        for n in range(rounds):
            try:
                result = await catalog.fetch(client)
                live = [m for m in result['matches'] if m['live']]
                print(f'round {n + 1}: {len(result["matches"])} games, {len(live)} live')
            except CatalogUnavailable as e:
                print(f'round {n + 1}: {e}')
            if n + 1 < rounds:
                await asyncio.sleep(interval)
    for name, row in catalog.health.summary(catalog.providers).items():
        print(f"{row['label']:12} score={row['score']:<5} ok={row['ok_rate']} live={row['live']} coverage={row['coverage']} "
              f"avg_coverage={row['avg_coverage']} latency={row['latency_ms']}ms {row['last_error'] or ''}")


if __name__ == '__main__':
    import sys
    asyncio.run(_probe(int(sys.argv[1]) if len(sys.argv) > 1 else 1, float(sys.argv[2]) if len(sys.argv) > 2 else 60))
