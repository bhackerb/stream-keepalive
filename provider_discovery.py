"""Weekly discovery reports from FMHY's Live Sports section. Never installs adapters.

Directory links are untrusted input. Probe only public HTTPS origins, resolve only
public IPs, do not follow redirects, and bound response sizes, time and concurrency.
Run on one controller; playback evidence remains device-local.
"""
import argparse
import asyncio
import ipaddress
import json
import os
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from aiohttp.resolver import DefaultResolver
from catalog_providers import PROVIDERS

DIRECTORY = 'https://fmhy.net/video'


class SportsLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.links = {}
        self.href = None
        self.label = []
        self.last_site_label = ''

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('h2', 'h3'):
            self.active = attrs.get('id') == 'live-sports'
        if self.active and tag == 'a':
            self.href = attrs.get('href')
            self.label = []

    def handle_data(self, data):
        if self.href:
            self.label.append(data)

    def handle_endtag(self, tag):
        if tag == 'a' and self.href:
            origin = public_origin(self.href)
            label = ''.join(self.label).strip()[:100]
            if origin and label.lower() not in ('status', 'mirrors', 'discord', 'telegram', 'guide', 'rss'):
                if label.isdigit() and self.last_site_label:
                    label = self.last_site_label + ' mirror ' + label
                elif label:
                    self.last_site_label = label
                self.links.setdefault(origin, label or origin)
            self.href = None


def public_origin(url):
    try:
        p = urlsplit(url)
        if (p.scheme != 'https' or not p.hostname or p.username is not None or p.password is not None or
                p.port not in (None, 443) or '.' not in p.hostname or
                any(c.isspace() or ord(c) < 32 for c in url)):
            return None
        host = p.hostname.lower()
        if host.endswith(('.local', '.internal', '.localhost', '.lan', '.home.arpa')):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            pass
        return urlunsplit(('https', host, '/', '', ''))
    except (ValueError, TypeError):
        return None


class PublicResolver(DefaultResolver):
    async def resolve(self, host, port=0, family=0):
        rows = await super().resolve(host, port, family)
        if not rows or any(not ipaddress.ip_address(row['host']).is_global for row in rows):
            raise OSError('Discovery refuses non-public network addresses')
        return rows


def configured_origins():
    origins = set()
    for provider in PROVIDERS:
        for url in provider.urls:
            origin = public_origin(url)
            origins.add(origin)
            if origin:
                host = urlsplit(origin).hostname
                if host.startswith(('www.', 'api.')):
                    origins.add('https://' + host.split('.', 1)[1] + '/')
    return origins


async def read_bounded(response, limit):
    data = bytearray()
    async for chunk in response.content.iter_chunked(16384):
        data.extend(chunk)
        if len(data) > limit:
            raise ValueError('Response exceeded discovery size limit')
    return data.decode('utf-8', errors='replace')


async def scan(client, previous=None, limit=20):
    previous = previous or {}
    async with client.get(DIRECTORY, allow_redirects=False) as response:
        if response.status != 200:
            raise ValueError(f'FMHY returned HTTP {response.status}; previous report preserved')
        parser = SportsLinks()
        parser.feed(await read_bounded(response, 2_000_000))
    if not parser.links:
        raise ValueError('FMHY Live Sports section missing or empty; previous report preserved')
    known = configured_origins()
    # Do not probe social/support links as streaming candidates.
    excluded = {'https://discord.gg/', 'https://discord.com/', 'https://t.me/',
                'https://github.com/', 'https://www.reddit.com/', 'https://fmhy.net/'}
    candidates = [(url, label) for url, label in parser.links.items()
                  if url not in known | excluded and url.replace('https://www.', 'https://', 1) not in known | excluded]
    old = previous.get('seen', {})
    # Rotate oldest probes so the fixed budget cannot starve later entries.
    candidates.sort(key=lambda item: old.get(item[0], 0))
    semaphore = asyncio.Semaphore(3)

    async def probe(url, label):
        async with semaphore:
            try:
                async with client.get(url, allow_redirects=False) as response:
                    await read_bounded(response, 512_000)
                    return dict(url=url, label=label, http_status=response.status,
                                state='needs_review' if response.status == 200 else 'unavailable_or_redirected',
                                new=url not in old, playback_verified=False)
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as exc:
                return dict(url=url, label=label, state='probe_failed', error=type(exc).__name__,
                            new=url not in old, playback_verified=False)

    rows = await asyncio.gather(*(probe(url, label) for url, label in candidates[:limit]))
    now = int(time.time())
    seen = {url: old.get(url, 0) for url, _ in candidates}
    seen.update({row['url']: now for row in rows})
    return dict(version=1, source=DIRECTORY+'#live-sports', scanned_at=now,
                listed_candidates=len(candidates), probed=len(rows), candidates=rows, seen=seen,
                authority='Report only. Adding providers requires reviewed code; no playback changes.')


def save_report(path, report):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as handle:
        os.fchmod(handle.fileno(), 0o600)
        json.dump(report, handle, indent=2)
    temp.replace(path)


async def main():
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument('--report', default='discovery-report.json')
    opts = args.parse_args()
    if os.environ.get('STREAMKEEPER_DISCOVERY_DISABLED') == '1':
        print('Provider discovery disabled')
        return
    path = Path(opts.report)
    try:
        previous = json.loads(path.read_text())
    except (OSError, ValueError):
        previous = {}
    connector = aiohttp.TCPConnector(resolver=PublicResolver())
    async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=10),
                                     headers={'User-Agent': 'StreamKeeper provider discovery (weekly)'}) as client:
        report = await asyncio.wait_for(scan(client, previous), 150)
    save_report(path, report)
    print(f"Probed {report['probed']} of {report['listed_candidates']} candidate sites; report: {path}")


if __name__ == '__main__':
    asyncio.run(main())
