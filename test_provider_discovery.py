import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from provider_discovery import SportsLinks, public_origin, PublicResolver, scan, save_report, DIRECTORY, configured_origins


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def page(self,text,status=200):
        r=MagicMock(status=status)
        async def chunks(*_):yield text.encode()
        r.content.iter_chunked=chunks
        cm=MagicMock();cm.__aenter__=AsyncMock(return_value=r);cm.__aexit__=AsyncMock(return_value=False)
        return cm

    def test_only_sports_https_origins_and_no_private_targets(self):
        p=SportsLinks();p.feed('<h3 id="movies">Movies</h3><a href="https://movie.example">Movie</a>'
            '<h3 id="live-sports">Sports</h3><a href="https://sports.example/path?q=x">Sports</a>'
            '<a href="https://sports.example/mirror">Mirror</a><a href="https://127.0.0.1/x">Bad</a>'
            '<h3 id="other">Other</h3><a href="https://other.example">Other</a>')
        self.assertEqual(p.links,{'https://sports.example/':'Sports'})
        for url in ['http://a.example','https://10.1.1.1','https://[::1]/','https://a.local/','https://u:p@a.example/','https://@a.example/','https://a.example:8890/']:
            self.assertIsNone(public_origin(url))
        self.assertIn('https://ppv.st/',configured_origins())
        self.assertIn('https://onhockey.tv/',configured_origins())

    async def test_probe_budget_rotation_and_report_only(self):
        html='<h3 id="live-sports">Sports</h3>'+''.join(f'<a href="https://p{i}.example/x">P{i}</a>'for i in range(4))
        client=MagicMock();client.get.side_effect=lambda url,**kw:self.page(html if url==DIRECTORY else 'hello')
        report=await scan(client,{'seen':{'https://p0.example/':100}},limit=2)
        self.assertEqual(report['probed'],2)
        self.assertEqual([x['url']for x in report['candidates']],['https://p1.example/','https://p2.example/'])
        self.assertTrue(all(not x['playback_verified']for x in report['candidates']))
        self.assertTrue(all(call.kwargs['allow_redirects']is False for call in client.get.call_args_list))
        with tempfile.TemporaryDirectory()as t:
            path=Path(t)/'report.json';save_report(path,report);self.assertEqual(path.stat().st_mode&0o777,0o600)
        client.get.side_effect=lambda *a,**kw:self.page('<h3>Changed layout</h3>')
        with self.assertRaises(ValueError):await scan(client)

    async def test_dns_private_results_are_rejected(self):
        with patch('provider_discovery.DefaultResolver.resolve',new=AsyncMock(return_value=[{'host':'127.0.0.1'}])):
            resolver=PublicResolver()
            try:
                with self.assertRaises(OSError):await resolver.resolve('public.example')
            finally:await resolver.close()
