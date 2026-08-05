import unittest

from sites import GenericStreamingDriver, OnHockeyTVDriver, StreamedPKDriver
from sites import get_driver, infer_site_from_url, is_streaming_url


class SiteInferenceTests(unittest.TestCase):
    def test_recognizes_supported_hosts_and_aliases(self):
        self.assertEqual(infer_site_from_url("https://streamed.pk/watch/1"), "streamed.pk")
        self.assertEqual(infer_site_from_url("https://www.vipbox.lc/hockey"), "viprow")
        self.assertEqual(infer_site_from_url("HTTPS://SPORTSURGE.NET/live"), "sportsurge")

    def test_does_not_match_paths_queries_or_unrelated_hosts(self):
        self.assertIsNone(infer_site_from_url("https://example.com/?next=sportsurge.net"))
        self.assertIsNone(infer_site_from_url("https://sportsurge.net.example.com/"))
        self.assertIsNone(infer_site_from_url("not a url"))
        self.assertIsNone(infer_site_from_url(""))
        self.assertFalse(is_streaming_url("https://example.com/streamed.pk"))

    def test_factory_selects_bespoke_and_generic_drivers(self):
        page = object()
        config = {}
        self.assertIsInstance(get_driver(page, config, "streamed.pk"), StreamedPKDriver)
        self.assertIsInstance(get_driver(page, config, "onhockey"), OnHockeyTVDriver)
        self.assertIsInstance(get_driver(page, config, "sportsurge"), GenericStreamingDriver)
        with self.assertRaises(ValueError):
            get_driver(page, config, "example.com")


if __name__ == "__main__":
    unittest.main()
