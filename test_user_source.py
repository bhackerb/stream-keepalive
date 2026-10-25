import unittest
from user_source import validate_user_source_url


class UserSourceUrlTests(unittest.TestCase):
    def test_supplied_game_pages_and_hash_routes_are_preserved(self):
        for url in ('https://olympicweb.me/milwaukee-brewers-vs-tampa-bay-rays-live-stream/2/',
                    'https://falconstreams.st/live/mlb/match/game/123',
                    'https://video.example.com:443/#/watch/game?quality=hd'):
            self.assertEqual(validate_user_source_url(url), url)

    def test_invalid_schemes_credentials_local_hosts_and_ambiguous_urls_are_rejected(self):
        for url in (None, 3, '', 'javascript:alert(1)', 'file:///tmp/video',
                    'http://example.com/', 'https://a:b@example.com/', 'https://@example.com/',
                    'https://localhost/', 'https://127.0.0.1/', 'https://[::1]/',
                    'https://0x7f.0x0.0x0.0x1/', 'https://0177.0.0.1/',
                    'https://example.local/', 'https://example.internal/', 'https://example.home.arpa/',
                    'https://example.com:8890/', 'https://example.com:bad/',
                    'https://example.com\\@other.com/', 'https://exam\nple.com/',
                    'https://example.com./', 'https://example.com/'+('x'*4096)):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_user_source_url(url)
