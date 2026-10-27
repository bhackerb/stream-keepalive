import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiohttp
from aiohttp.test_utils import TestClient, TestServer

import catalog_providers as cp
from api_server import StreamKeeperAPI
import test_regular_browser as fixtures

NOW = 1793070000_000  # 2026-10-27 03:20 UTC; Nuggets-Warriors tipped off at 02:00 UTC

PPV = {'streams': [
    {'category': 'Basketball', 'streams': [
        {'id': 1, 'name': 'Golden State Warriors vs. Denver Nuggets', 'tag': 'NBA', 'source_tag': 'ESPN', 'category_name': 'Basketball',
         'starts_at': 1793066400, 'ends_at': 1793077200, 'always_live': 0, 'locale': 'en', 'viewers': '6117',
         'iframe': 'https://rotating.example/embed/nba/gs-den',
         'substreams': [{'id': 2, 'source_tag': 'ESPN Deportes', 'locale': 'es', 'iframe': 'https://rotating.example/embed/gs-den-es'}]},
    ]},
    {'category': '24/7 Streams', 'streams': [
        {'id': 3, 'name': 'NFL Network', 'always_live': 1, 'starts_at': 0, 'ends_at': 0, 'iframe': 'https://rotating.example/embed/nfln'},
    ]},
    {'category': 'Wrestling', 'streams': [
        {'id': 4, 'name': 'WWE NXT', 'tag': 'Wrestling', 'category_name': 'Wrestling', 'starts_at': 1793145600, 'ends_at': 1793156400,
         'always_live': 0, 'viewers': '0', 'iframe': 'https://rotating.example/embed/wwe'},
    ]},
]}
STREAMFREE = {'streams': [
    {'id': 'golden-state-warriors-vs-denver-nuggets', 'name': 'Golden State Warriors vs Denver Nuggets', 'category': 'basketball', 'league': 'NBA',
     'match_timestamp': 1793066400, 'viewers': 2764, 'sources': ['https://strmfree.example/embed/gsw-den1080p'],
     'team1': {'name': 'Golden State Warriors'}, 'team2': {'name': 'Denver Nuggets'}},
    {'id': 'redzone', 'name': 'RedZone', 'category': 'football', 'match_timestamp': 1789230600, 'viewers': 6, 'sources': ['https://strmfree.example/embed/rz']},
]}
TIMSTREAMS = {'genres': [{'id': 7, 'name': 'Basketball'}, {'id': 11, 'name': 'Hockey'}], 'events': [
    {'url': 'gsw-den', 'name': 'Golden State Warriors @ Denver Nuggets', 'genre': 7, 'time': '2026-10-26T22:00', 'vip': False, 'viewers': 0,
     'streams': [{'name': 'NBC Sports', 'url': 'https://tim.example/x1', 'vip': False}, {'name': 'VIP', 'url': 'https://tim.example/vip', 'vip': True}]},
    {'url': 'tor-cgy', 'name': 'Toronto Maple Leafs @ Calgary Flames', 'genre': 11, 'time': '2026-10-26T20:30', 'vip': False, 'viewers': 0,
     'streams': [{'name': 'Sportsnet', 'url': 'https://tim.example/x2', 'vip': False}]},
]}
ROCKYLIVE = {'matches': [
    {'id': 9, 'category': 'basketball', 'league': 'nba', 'title': 'Denver Nuggets - Golden State Warriors', 'ts_et': 1793066400, 'status': 'LIVE',
     'streams': [{'hd': 516, 'link': 'https://rockyembed.example/fetch.php?hd=516'}]},
    {'id': 10, 'category': 'hockey', 'league': 'nhl', 'title': 'Calgary Flames - Toronto Maple Leafs', 'ts_et': 1793061000, 'status': 'LIVE',
     'streams': [{'hd': 1, 'link': 'https://rockyembed.example/fetch.php?hd=1'}]},
]}
CDN = {'cdn-live-tv': {'NHL': [
    {'gameID': 'g1', 'event': 'Calgary Flames vs Toronto Maple Leafs', 'homeTeam': 'Calgary Flames', 'awayTeam': 'Toronto Maple Leafs',
     'start': '2026-10-27 00:30', 'end': '2026-10-27 03:30', 'status': 'P3', 'tournament': 'NHL',
     'channels': [{'channel_name': 'Sportsnet One', 'viewers': 12, 'url': 'https://cdn.example/player/?name=SN%201'}]},
    {'gameID': 'g2', 'event': 'Old Game', 'homeTeam': 'A', 'awayTeam': 'B', 'start': '2026-10-26 18:00', 'status': 'FT',
     'channels': [{'channel_name': 'X', 'url': 'https://cdn.example/old'}]},
], 'total_events_nhl': 2}}
ONHOCKEY = """<table id='gametable'>
<tbody class='NAp'><tr><td></td><td><b>NHL</b></td><td></td></tr>
<tr class='game' onclick='javascript:Menu(this)'><td><text class='game_hour' align='left'>00</text>:30</td><td>Toronto - Calgary Flames
<div class='gamelinks' onclick='event.stopPropagation()'>
english:
<a href='np_stream400.php?channel=//embed.example/nhl/tor-cgy/1' target='player_frame' title='SN+'>streamd</a>
<a href='np_stream400.php?channel=//other.example/ch?id=4' target='player_frame' title='ESPN+'>vertex</a>
<br>russian:
<a href='np_stream400.php?channel=//ru.example/x' target='player_frame' title=''>embedv</a>
<a href='np_vk.php?channel=-41593209%26id=456254371' target='player_frame' title=''>vklive</a>
</div></td></tr>
<tbody class='FI'><tr><td></td><td><b>Liiga</b></td><td></td></tr>
<tr class='game' onclick='javascript:Menu(this)'><td><text class='game_hour' align='left'>16</text>:30</td><td>KalPa - Vaasan Sport
<div class='gamelinks' onclick='event.stopPropagation()'>
<a href='np_stream400.php?channel=//fi.example/kalpa' target='player_frame' title='Ruutu'>ruutu</a>
</div></td></tr>
<tr class='game' onclick='javascript:Menu(this)'><td><text class='game_hour' align='left'>17</text>:00</td><td>No Links - Yet
<div class='gamelinks' onclick='event.stopPropagation()'>
</div></td></tr>
</table>"""
PAYLOADS = {'ppv': PPV, 'streamfree': STREAMFREE, 'timstreams': TIMSTREAMS, 'rockylive': ROCKYLIVE, 'cdnlivetv': CDN, 'onhockey': ('text/html', ONHOCKEY)}


def fake_client(routes):
    """routes: url -> payload | Exception | ('text/html', None)."""
    def get(url, **_):
        value = routes.get(url, aiohttp.ClientConnectionError('unreachable'))
        cm = MagicMock()
        if isinstance(value, Exception):
            cm.__aenter__ = AsyncMock(side_effect=value)
        else:
            content_type, payload = value if isinstance(value, tuple) else ('application/json', value)
            res = MagicMock(content_type=content_type, raise_for_status=MagicMock(), json=AsyncMock(return_value=payload), text=AsyncMock(return_value=payload))
            cm.__aenter__ = AsyncMock(return_value=res)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm
    return MagicMock(get=MagicMock(side_effect=get))


def all_routes(**overrides):
    routes = {p.urls[0]: PAYLOADS[p.name] for p in cp.PROVIDERS}
    routes.update(overrides)
    return routes


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.catalog = cp.Catalog(Path(self.tmp.name) / 'health.json')

    async def fetch(self, routes=None):
        return await self.catalog.fetch(fake_client(routes or all_routes()), now_ms=NOW)

    def game(self, result, *words):
        return next(m for m in result['matches'] if all(w in m['id'] for w in words))

    async def test_same_game_merges_across_providers_with_flipped_home_away(self):
        result = await self.fetch()
        nba = self.game(result, 'nuggets', 'warriors')
        self.assertEqual(nba['providers'], ['ppv', 'rockylive', 'streamfree', 'timstreams'])
        self.assertEqual(nba['id'], '2026-10-26-nuggets-warriors')
        self.assertTrue(nba['live'])
        self.assertEqual(nba['category'], 'basketball')
        self.assertEqual(nba['viewers'], 6117)
        self.assertNotIn('https://tim.example/vip', [s['url'] for s in nba['sources']])
        nhl = self.game(result, 'flames', 'leafs')
        self.assertEqual(nhl['providers'], ['cdnlivetv', 'onhockey', 'rockylive', 'timstreams'])
        self.assertEqual(nhl['title'], 'Toronto Maple Leafs @ Calgary Flames')  # fullest names win over "Toronto"

    async def test_channels_stale_and_finished_events_are_not_games(self):
        result = await self.fetch()
        titles = [m['title'] for m in result['matches']]
        for skipped in ('NFL Network', 'RedZone', 'Old Game'):
            self.assertNotIn(skipped, titles)
        wrestling = self.game(result, 'wwe')
        self.assertEqual(wrestling['category'], 'wrestling')
        self.assertFalse(wrestling['live'])

    async def test_sources_rank_by_viewers_then_english_without_history(self):
        nba = self.game(await self.fetch(), 'nuggets')
        self.assertEqual(nba['sources'][0]['url'], 'https://rotating.example/embed/nba/gs-den')
        self.assertEqual(nba['sources'][1]['url'], 'https://strmfree.example/embed/gsw-den1080p')
        spanish = next(s for s in nba['sources'] if s['language'] == 'es')
        self.assertEqual(spanish['provider'], 'ppv')

    async def test_consistent_provider_outranks_viewer_counts_once_evidence_exists(self):
        for _ in range(3):
            await self.fetch(all_routes(**{cp.PROVIDERS[0].urls[0]: aiohttp.ClientConnectionError('down'),
                                          cp.PROVIDERS[0].urls[1]: aiohttp.ClientConnectionError('down')}))
        nba = self.game(await self.fetch(), 'nuggets')
        self.assertNotEqual(nba['sources'][0]['provider'], 'ppv')
        self.assertLess(self.catalog.health.score('ppv'), self.catalog.health.score('rockylive'))

    async def test_one_provider_down_or_walled_only_drops_its_sources(self):
        routes = all_routes(**{cp.PROVIDERS[0].urls[0]: ('text/html', None), cp.PROVIDERS[0].urls[1]: ('text/html', None)})
        result = await self.fetch(routes)
        self.assertNotIn('ppv', self.game(result, 'nuggets')['providers'])
        health = result['providers']['ppv']
        self.assertFalse(health['last_ok'])
        self.assertIn('text/html', health['last_error'])

    async def test_mirror_is_used_when_primary_fails(self):
        ppv = cp.PROVIDERS[0]
        result = await self.fetch(all_routes(**{ppv.urls[0]: aiohttp.ClientConnectionError('down'), ppv.urls[1]: PPV}))
        self.assertEqual(result['providers']['ppv']['mirror'], ppv.urls[1])
        self.assertIn('ppv', self.game(result, 'nuggets')['providers'])

    async def test_schema_change_is_a_provider_failure_not_a_crash(self):
        result = await self.fetch(all_routes(**{cp.PROVIDERS[3].urls[0]: {'matches': 'nonsense'}, cp.PROVIDERS[4].urls[0]: ['not', 'a', 'dict']}))
        self.assertTrue(result['providers']['rockylive']['last_ok'])  # tolerated: no usable rows
        self.assertFalse(result['providers']['cdnlivetv']['last_ok'])

    async def test_every_provider_down_raises_catalog_unavailable_and_records_health(self):
        with self.assertRaises(cp.CatalogUnavailable):
            await self.catalog.fetch(fake_client({}), now_ms=NOW)
        self.assertEqual(len(self.catalog.health.polls['ppv']), 1)
        with self.assertRaises(cp.CatalogUnavailable):
            await self.catalog.fetch(None)

    async def test_ids_stay_stable_when_the_naming_provider_drops_out(self):
        first = self.game(await self.fetch(), 'nuggets')['id']
        routes = all_routes(**{p.urls[0]: aiohttp.ClientConnectionError('down') for p in cp.PROVIDERS if p.name in ('ppv', 'streamfree')})
        routes.update({cp.PROVIDERS[0].urls[1]: aiohttp.ClientConnectionError('down'), cp.PROVIDERS[1].urls[1]: aiohttp.ClientConnectionError('down')})
        again = self.game(await self.fetch(routes), 'nuggets')
        self.assertEqual(again['id'], first)

    async def test_onhockey_wrapper_links_languages_and_specialist_coverage(self):
        result = await self.fetch()
        nhl = self.game(result, 'flames', 'leafs')
        onhockey = [s for s in nhl['sources'] if s['provider'] == 'onhockey']
        self.assertEqual([s['url'] for s in onhockey], ['https://www.onhockey.tv/np_stream400.php?channel=//embed.example/nhl/tor-cgy/1',
                                                       'https://www.onhockey.tv/np_stream400.php?channel=//other.example/ch?id=4',
                                                       'https://www.onhockey.tv/np_stream400.php?channel=//ru.example/x'])
        self.assertEqual([s['language'] for s in onhockey], ['English', 'English', 'Russian'])
        self.assertEqual(onhockey[0]['label'], 'SN+ · streamd')
        liiga = self.game(result, 'kalpa')
        self.assertEqual((liiga['category'], liiga['live']), ('hockey', False))
        self.assertNotIn('No Links - Yet', [m['title'] for m in result['matches']])
        self.assertEqual(result['providers']['onhockey']['coverage'], 1.0)  # scored only against live hockey

    def test_onhockey_hours_resolve_to_the_nearest_day(self):
        page = ONHOCKEY.replace('>00</text>:30', '>23</text>:30')
        just_after_midnight = 1793059800_000  # 2026-10-27 00:10 UTC
        game = next(e for e in cp.parse_onhockey(page, just_after_midnight) if e and 'Toronto' in e['title'])
        self.assertEqual(game['start_ms'], just_after_midnight - 40 * 60000)
        kalpa = next(e for e in cp.parse_onhockey(ONHOCKEY, NOW) if e and 'KalPa' in e['title'])
        self.assertEqual(kalpa['start_ms'], 1793118600_000)  # 16:30 UTC later the same day

    async def test_ids_survive_restart_and_short_name_listings(self):
        first = self.game(await self.fetch(), 'flames', 'leafs')['id']
        restarted = cp.Catalog(Path(self.tmp.name) / 'health.json')
        only_short = {cp.PROVIDERS[-1].urls[0]: ('text/html', ONHOCKEY)}
        again = await restarted.fetch(fake_client(only_short), now_ms=NOW + 60000)
        self.assertEqual(next(m for m in again['matches'] if 'Toronto' in m['title'])['id'], first)

    async def test_health_persists_and_reports_coverage(self):
        await self.fetch()
        reloaded = cp.Catalog(Path(self.tmp.name) / 'health.json')
        summary = reloaded.health.summary(reloaded.providers)
        self.assertEqual(summary['rockylive']['live'], 2)
        self.assertEqual(summary['onhockey']['live'], 1)
        self.assertEqual(summary['rockylive']['coverage'], 1.0)
        self.assertEqual(summary['streamfree']['coverage'], .5)

    def test_category_labels(self):
        self.assertEqual(cp.category_of('Australian Football', 'AFL'), 'other')
        self.assertEqual(cp.category_of('American Football', 'NFL'), 'american-football')
        self.assertEqual(cp.category_of('Football', 'Serie A'), 'football')
        self.assertEqual(cp.category_of('Ice Hockey'), 'hockey')
        self.assertEqual(cp.category_of('Mixed Martial Arts'), 'fight')
        self.assertEqual(cp.category_of('Profesional Wrestling'), 'wrestling')
        self.assertEqual(cp.category_of('Motorsport'), 'motor-sports')

    def test_different_games_do_not_merge(self):
        base = dict(provider='x', pid='1', category='hockey', start_ms=NOW, end_ms=None, live=None, viewers=None, streams=[])
        a = dict(base, title='Calgary Flames - Toronto Maple Leafs', teams=['Calgary Flames', 'Toronto Maple Leafs'])
        b = dict(base, title='Calgary Flames - Edmonton Oilers', teams=['Calgary Flames', 'Edmonton Oilers'])
        later = dict(a, start_ms=NOW + 4 * 3600_000)
        self.assertFalse(cp.same_game(a, b))
        self.assertFalse(cp.same_game(a, later))


class KeeperCatalogTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.RegularTests.asyncSetUp
    asyncTearDown = fixtures.RegularTests.asyncTearDown

    async def test_catalog_url_opens_as_user_source_and_stays_recoverable(self):
        self.k.catalog.urls = {'https://rotating.example/embed/x'}
        self.k._companion_info = {'firefox': {'capabilities': ['user-source-v1']}}
        self.k.command = AsyncMock(return_value={'url': 'https://rotating.example/embed/x', 'userSource': True})
        await self.k._open('g', 'https://rotating.example/embed/x', 'Game', 'firefox', 's')
        self.assertIs(self.k.command.call_args.kwargs['user_source'], True)
        self.assertEqual(self.k._recovery['g']['catalog'], True)
        self.assertTrue(self.k.active_streams['g'].user_source)
        restored = fixtures.RegularBrowserKeeper(self.config)
        self.assertTrue(restored._recovery['g']['catalog'])

    async def test_catalog_url_requires_user_source_companion(self):
        self.k.catalog.urls = {'https://rotating.example/embed/x'}
        with self.assertRaisesRegex(ValueError, 'catalog sources'):
            await self.k._open('g', 'https://rotating.example/embed/x', 'Game', 'firefox', 's')
        self.k.command.assert_not_awaited()

    async def test_unknown_non_allowlisted_url_is_still_rejected(self):
        with self.assertRaisesRegex(ValueError, 'allowlisted'):
            await self.k._open('g', 'https://unknown.example/embed/x', 'Game', 'firefox', 's')

    async def test_session_returns_502_with_reason_when_every_provider_fails(self):
        self.k._client = fake_client({})
        async with TestClient(TestServer(StreamKeeperAPI(self.k, self.config).app)) as client:
            resp = await client.post('/session', json={'action': 'plan', 'sports': ['hockey']}, headers={'Authorization': 'Bearer test-key'})
            body = await resp.json()
        self.assertEqual(resp.status, 502)
        self.assertIn('No stream catalog reachable', body['message'])
        self.assertIn('existing streams preserved', body['message'])

    async def test_status_reports_provider_consistency(self):
        self.k._client = fake_client(all_routes())
        await self.k.catalog.fetch(self.k._client, now_ms=NOW)
        providers = self.k.extra_status()['catalog']['providers']
        self.assertEqual(set(providers), {p.name for p in cp.PROVIDERS})
        self.assertTrue(providers['ppv']['last_ok'])


if __name__ == '__main__':
    unittest.main()
