import unittest
from unittest.mock import AsyncMock, patch

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


class GenericSourceIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_source_clicks_its_stable_locator(self):
        page = AsyncMock()
        page.evaluate.return_value = [
            {"name": "Mirror A", "href": None, "index": 0, "locator": "streamkeeper-0"},
            {"name": "Mirror B", "href": None, "index": 1, "locator": "streamkeeper-1"},
        ]
        second_source = AsyncMock()
        page.query_selector.return_value = second_source

        driver = GenericStreamingDriver(page, {}, "sportsurge")
        driver._dismiss_initial_ads = AsyncMock()
        driver._has_video_element = AsyncMock(return_value=True)

        await driver.list_sources()
        with patch("sites.asyncio.sleep", new=AsyncMock()):
            self.assertTrue(await driver.load_stream(1))

        page.query_selector.assert_awaited_once_with(
            '[data-streamkeeper-source-id="streamkeeper-1"]'
        )
        second_source.click.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
