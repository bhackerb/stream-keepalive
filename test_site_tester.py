import unittest

import site_tester as t


class CandidateScoringTests(unittest.TestCase):
    def test_game_pages_beat_listings_and_ads(self):
        game = t.score('https://x.example/watch/nhl-rangers-vs-canucks-123', 'Rangers vs Canucks LIVE', 'x.example')
        listing = t.score('https://x.example/events', 'Events', 'x.example')
        junk = t.score('https://x.example/login', 'Login', 'x.example')
        self.assertGreaterEqual(game, 3)
        self.assertLess(listing, 3)
        self.assertEqual(junk, -1)

    def test_fmhy_section_parser_keeps_sites_and_drops_social(self):
        html = ('<h2 id="live-tv-sports">x</h2><h3 id="live-tv">Live TV</h3><a href="https://a.example/">A</a>'
                '<a href="https://discord.gg/x">Discord</a><h3 id="live-sports">Live Sports</h3><a href="https://b.example/p">B</a>'
                '<h3 id="other">Other</h3><a href="https://c.example/">C</a>')
        parser = t.SectionLinks()
        parser.feed(html)
        self.assertEqual(sorted(parser.sites), ['a.example', 'b.example'])
        self.assertEqual(parser.sites['a.example']['section'], 'live-tv')


if __name__ == '__main__':
    unittest.main()
