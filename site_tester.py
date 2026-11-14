"""Automated site tester: every site on FMHY's "Live TV / Sports" list, opened for real on the TV box.

For each reachable site it finds candidate live pages, opens one as a StreamKeeper probe (full-TV placement,
muted, never auto-recovered), waits for real video, samples buffering, checks that the player really fills
the window, then closes it. Results append to site-tests.jsonl; site-test-report.json/.md summarize the
latest result per site and how often each has worked.

  python3 site_tester.py                 # everything reachable (nightly timer)
  python3 site_tester.py --sites a.com   # only matching hosts
  python3 site_tester.py --limit 5       # first N sites
Never runs while real streams are active.
"""
import argparse
import asyncio
import json
import re
import time
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import aiohttp

HERE = Path(__file__).resolve().parent
DIRECTORY = 'https://fmhy.net/video'
API = 'http://127.0.0.1:8890'
UA = 'Mozilla/5.0 (X11; Linux x86_64; rv:153.0) Gecko/20100101 Firefox/153.0'
SECTIONS = ('live-tv', 'live-sports')
SKIP_LABELS = {'status', 'mirrors', 'discord', 'telegram', 'guide', 'rss', 'reddit', 'subreddit', 'twitter', 'github'}
SKIP_HOSTS = ('discord.', 't.me', 'telegram.', 'reddit.', 'twitter.', 'x.com', 'github.', 'youtube.', 'facebook.', 'instagram.', 'fmhy.')
HINT = re.compile(r'watch|stream|live|match|event|game|channel|embed|play|/nhl|/nba|/nfl|/mlb|/soccer|/hockey|/tv', re.I)
AVOID = re.compile(r'login|register|signup|contact|dmca|privacy|terms|about|faq|discord|telegram|\.(png|jpg|css|js|svg)(\?|$)|#$', re.I)
PLAY_TIMEOUT, SAMPLE_SECONDS, MAX_CANDIDATES = 50, 40, 3


class SectionLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.section, self.href, self.label, self.sites = None, None, [], {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('h2', 'h3'):
            self.section = attrs.get('id') if attrs.get('id') in SECTIONS else (None if attrs.get('id') else self.section)
        if self.section and tag == 'a' and attrs.get('href', '').startswith('https://'):
            self.href, self.label = attrs['href'], []

    def handle_data(self, data):
        if self.href:
            self.label.append(data)

    def handle_endtag(self, tag):
        if tag == 'a' and self.href:
            label = ''.join(self.label).strip()[:80]
            host = urlsplit(self.href).hostname or ''
            if host and label.lower() not in SKIP_LABELS and not any(s in host for s in SKIP_HOSTS):
                self.sites.setdefault(host.removeprefix('www.'), dict(url=f'https://{host}/', label=label or host, section=self.section))
            self.href = None


class PageLinks(HTMLParser):
    def __init__(self, base):
        super().__init__()
        self.base, self.links, self.frames, self._a, self._text = base, [], [], None, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a' and attrs.get('href'):
            self._a, self._text = urljoin(self.base, attrs['href']), []
        if tag == 'iframe' and attrs.get('src', '').startswith(('http', '//')):
            self.frames.append(urljoin(self.base, attrs['src']))

    def handle_data(self, data):
        if self._a:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == 'a' and self._a:
            self.links.append((self._a, ' '.join(''.join(self._text).split())[:100]))
            self._a = None


def score(url, text, host):
    if AVOID.search(url) or not url.startswith('https://'):
        return -1
    s = 0
    same = (urlsplit(url).hostname or '').removeprefix('www.').endswith(host.split('.', 1)[-1] if host.count('.') > 1 else host)
    s += 2 if same else -2
    s += 2 if HINT.search(url) else 0
    s += 3 if re.search(r'\blive\b|●|now|\bvs\b|\bv\b| - ', text, re.I) else 0
    s += 1 if text else 0
    path = urlsplit(url).path.strip('/')
    s += 1 if path.count('/') >= 1 or re.search(r'\d', path) else 0
    return s


async def fetch(session, url, timeout=15, ua=UA):
    try:
        async with session.get(url, headers={'User-Agent': ua}, timeout=aiohttp.ClientTimeout(total=timeout), allow_redirects=True) as res:
            body = (await res.read())[:900_000].decode(res.get_encoding() or 'utf-8', 'ignore')
            return res.status, str(res.url), body
    except Exception as e:
        return None, url, type(e).__name__


async def load_sites(session):
    status, _, body = await fetch(session, DIRECTORY, 30, ua='curl/8.5.0')  # browser UAs get an unrendered SPA shell
    if status != 200:
        raise RuntimeError(f'FMHY unavailable: {status} {body[:80]}')
    parser = SectionLinks()
    parser.feed(body)
    return parser.sites


async def catalog_index(session):
    """host -> live catalog source URLs, so sites we already ingest are tested on real live games."""
    import catalog_providers as cp
    catalog = cp.Catalog(HERE / 'provider-health.json')
    data = await catalog.fetch(session)
    index = defaultdict(list)
    mirrors = {p.name: {(urlsplit(u).hostname or '').removeprefix('www.') for u in p.urls} for p in catalog.providers}
    for match in sorted(data['matches'], key=lambda m: not m['live']):
        if not match['live']:
            continue
        for src in match['sources']:
            index[(urlsplit(src['url']).hostname or '').removeprefix('www.')].append(src['url'])
            for host in mirrors.get(src['provider'], ()):
                index[host].append(src['url'])
    return index


async def candidates(session, host, home_url, index=None):
    status, final, body = await fetch(session, home_url)
    info = dict(status=status, final_url=final if status else None)
    known = []
    for h in (host, (urlsplit(final).hostname or '').removeprefix('www.')):
        known += [u for u in (index or {}).get(h, []) if u not in known]
    if known:
        info.update(status=status, tier='catalog', candidates=min(len(known), MAX_CANDIDATES))
        return info, known[:MAX_CANDIDATES]
    if status != 200 or len(body) < 300:
        info['error'] = body[:60] if status is None else f'HTTP {status}'
        return info, []
    final_host = (urlsplit(final).hostname or host).removeprefix('www.')
    page = PageLinks(final)
    page.feed(body)
    ranked = {}
    for url, text in page.links:
        sc = score(url, text, final_host)
        if sc >= 3:
            ranked[url.split('#')[0]] = max(sc, ranked.get(url.split('#')[0], 0))
    for m in re.finditer(r'["\'](/(?:watch|stream|match|event|embed|game|live|channel)[^"\'\s<>]{3,90})["\']', body):
        url = urljoin(final, m.group(1)).split('#')[0]
        ranked.setdefault(url, 3)
    cands = [u for u, _ in sorted(ranked.items(), key=lambda kv: -kv[1])][:MAX_CANDIDATES]
    for frame in page.frames[:2]:
        if frame.startswith('https://') and frame not in cands:
            cands.append(frame)
    if not cands and final.startswith('https://'):
        cands = [final]
    info['candidates'] = len(cands)
    return info, cands


async def api(session, method, path, key, **kw):
    async with session.request(method, API + path, headers={'Authorization': f'Bearer {key}'}, timeout=aiohttp.ClientTimeout(total=30), **kw) as res:
        return await res.json()


async def run_probe(session, key, url):
    """Open one URL on the TV and judge it. Returns a result dict; always closes the probe."""
    out = dict(url=url)
    started = time.time()
    opened = await api(session, 'POST', '/probe', key, json=dict(url=url, label='site test'))
    if not opened.get('success'):
        return dict(out, outcome='open_failed', message=opened.get('message', '')[:120])
    sid = opened['stream_id']
    try:
        playing_at, samples, buffering, last = None, 0, 0, {}
        while time.time() - started < PLAY_TIMEOUT + SAMPLE_SECONDS + 5:
            await asyncio.sleep(2)
            rep = await api(session, 'GET', f'/probe/{sid}', key)
            if not rep.get('present'):
                return dict(out, outcome='tab_lost')
            last = rep
            frames = rep.get('frames', {}).values()
            if any(f.get('page_error') for f in frames) and not playing_at:
                return dict(out, outcome='dead_page', seconds=round(time.time() - started))
            state = rep['health'].get('state')
            if state == 'PLAYING' and playing_at is None:
                playing_at = time.time()
            if playing_at:
                samples += 1
                buffering += state in ('BUFFERING', 'LOADING')
                if time.time() - playing_at >= SAMPLE_SECONDS:
                    break
            elif time.time() - started > PLAY_TIMEOUT:
                break
        health = last.get('health', {})
        if not playing_at:
            return dict(out, outcome='no_video', autoplay=health.get('autoplay'), fill=last.get('fill'), seconds=round(time.time() - started))
        return dict(out, outcome='playing', seconds_to_play=round(playing_at - started, 1), resolution=health.get('resolution'),
                    buffering_pct=round(100 * buffering / max(samples, 1), 1), buffer_episodes=(last.get('buffering') or {}).get('episodes', 0),
                    fill=last.get('fill'), window_fullscreen=last.get('window_fullscreen'), autoplay=health.get('autoplay'))
    finally:
        try:
            await api(session, 'DELETE', f'/probe/{sid}', key)
        except Exception:
            pass


def api_key():
    import re as _re
    text = (HERE / 'config.yaml').read_text()
    return _re.search(r'^\s*api_key:\s*["\']?([^"\'\s]+)', text, _re.M).group(1)


def summarize():
    rows = defaultdict(list)
    for line in (HERE / 'site-tests.jsonl').read_text().splitlines()[-20000:] if (HERE / 'site-tests.jsonl').exists() else []:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        rows[r['site']].append(r)
    report = []
    for site, rs in rows.items():
        last = rs[-1]
        ok = sum(r['outcome'] == 'playing' for r in rs)
        report.append(dict(site=site, label=last.get('label'), last_at=last['at'], last_outcome=last['outcome'], runs=len(rs), played=ok,
                           pass_rate=round(100 * ok / len(rs)), last=last))
    report.sort(key=lambda r: (-r['pass_rate'], r['site']))
    (HERE / 'site-test-report.json').write_text(json.dumps(report, indent=1))
    lines = [f"# FMHY site test report ({time.strftime('%Y-%m-%d %H:%M')})", '', '| Site | Last | Pass | Time to play | Buffering | Resolution | Fill | Fullscreen | Detail |', '|---|---|---|---|---|---|---|---|---|']
    for r in report:
        x = r['last']
        lines.append(f"| {r['site']} | {r['last_outcome']} | {r['played']}/{r['runs']} | {x.get('seconds_to_play', '')} | {x.get('buffering_pct', '')} | {x.get('resolution', '')} | {x.get('fill', '')} | {x.get('window_fullscreen', '')} | {x.get('message') or x.get('error') or ''} |")
    (HERE / 'SITE_TEST_REPORT.md').write_text('\n'.join(lines) + '\n')
    return report


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sites', nargs='*')
    ap.add_argument('--limit', type=int)
    ap.add_argument('--budget-minutes', type=int, default=180)
    args = ap.parse_args()
    key, deadline = api_key(), time.time() + args.budget_minutes * 60
    async with aiohttp.ClientSession() as session:
        status = await api(session, 'GET', '/status', key)
        if any(not s['stream_id'].startswith('probe-') for s in status.get('streams', [])):
            print('Real streams are active; not testing.')
            return
        if not status.get('browser_connections', {}).get('firefox'):
            print('Firefox companion is not connected; not testing.')
            return
        sites = await load_sites(session)
        if args.sites:
            sites = {h: v for h, v in sites.items() if any(s in h for s in args.sites)}
        names = list(sites)[:args.limit] if args.limit else list(sites)
        print(f'{len(names)} sites from FMHY')
        index = await catalog_index(session)
        sem = asyncio.Semaphore(10)

        async def scout(host):
            async with sem:
                return host, *await candidates(session, host, sites[host]['url'], index)
        scouted = await asyncio.gather(*(scout(h) for h in names))
        for host, info, cands in scouted:
            if time.time() > deadline:
                print('Time budget reached')
                break
            base = dict(at=int(time.time()), site=host, label=sites[host]['label'], section=sites[host]['section'], **info)
            result = None
            for url in cands:
                if any(not s['stream_id'].startswith('probe-') for s in (await api(session, 'GET', '/status', key)).get('streams', [])):
                    print('Real stream appeared; stopping.')
                    summarize()
                    return
                result = await run_probe(session, key, url)
                print(f"{host:28}{result['outcome']:12}{url[:70]}")
                if result['outcome'] == 'playing':
                    break
            if result is None:
                result = dict(outcome='unreachable' if info.get('status') != 200 else 'no_candidates')
                print(f"{host:28}{result['outcome']}")
            with (HERE / 'site-tests.jsonl').open('a') as f:
                f.write(json.dumps({**base, **result}) + '\n')
    summarize()


if __name__ == '__main__':
    asyncio.run(main())
