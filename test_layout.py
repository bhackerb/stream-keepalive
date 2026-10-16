import unittest
from unittest.mock import AsyncMock
from types import SimpleNamespace

from display_layout import plan_layout
from ad_handler import AdHandler
from stream_keeper import StreamKeeper

DISPLAYS = {"tv": {"x": 0, "y": 0, "width": 3840, "height": 2160},
            "widescreen": {"x": -3440, "y": 0, "width": 3440, "height": 1440},
            "vertical": {"x": 0, "y": -1920, "width": 1080, "height": 1920}}


class LayoutTests(unittest.TestCase):
    def test_requested_capacities_and_even_coverage(self):
        for display, count in [("tv", 1), ("widescreen", 6), ("vertical", 4), ("dock", 10)]:
            placements = plan_layout(DISPLAYS, display, "grid", [str(i) for i in range(count)])
            self.assertEqual(len(placements), count)
            for name in {p["display"] for p in placements}:
                region = DISPLAYS[name]
                cells = [p for p in placements if p["display"] == name]
                self.assertEqual(sum(p["width"] * p["height"] for p in cells), region["width"] * region["height"])
                for i, a in enumerate(cells):
                    for b in cells[i+1:]:
                        self.assertTrue(a["x"] + a["width"] <= b["x"] or b["x"] + b["width"] <= a["x"] or
                                        a["y"] + a["height"] <= b["y"] or b["y"] + b["height"] <= a["y"])

    def test_partial_row_odd_pixels_and_negative_origins(self):
        displays = {"widescreen": {"x": -5, "y": 20, "width": 101, "height": 99}}
        cells = plan_layout(displays, "widescreen", "grid", ["a", "b", "c"])
        self.assertEqual(cells[-1]["width"], 101)
        self.assertEqual(sum(p["width"] * p["height"] for p in cells), 9999)

    def test_focus_side_by_side_and_rejected_overflow(self):
        self.assertEqual(plan_layout(DISPLAYS, "tv", "focus", ["a"])[0]["width"], 3840)
        self.assertEqual([p["width"] for p in plan_layout(DISPLAYS, "widescreen", "side-by-side", ["a", "b"])], [1720, 1720])
        for args in [("tv", "focus", ["a", "b"]), ("tv", "grid", ["a"] * 2), ("missing", "grid", ["a"]), ("vertical", "side-by-side", ["a"])]:
            with self.assertRaises(ValueError):
                plan_layout(DISPLAYS, *args)


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_shared_window_rejected_before_moving(self):
        keeper = StreamKeeper({"browser": {"engine": "chromium"}, "layout": {"displays": DISPLAYS}})
        session = AsyncMock()
        session.send.return_value = {"windowId": 1}
        keeper.context = AsyncMock()
        keeper.context.new_cdp_session.return_value = session
        keeper.active_streams = {s: SimpleNamespace(page=object()) for s in ["a", "b"]}
        result = await keeper.arrange_streams("widescreen", "grid")
        self.assertFalse(result["success"])
        self.assertEqual([call.args[0] for call in session.send.await_args_list], ["Browser.getWindowForTarget"] * 2)

    async def test_ad_domains_block_but_video_paths_do_not(self):
        handler = AdHandler({"ads": {"blocked_domain_patterns": ["*stake.com*"]}})
        for url, blocked in [("https://doubleclick.net/a", True), ("https://cdn.example/video/ad-segment.ts", False),
                             ("https://cloudfront.net/stream/adaptive.m3u8", False), ("https://stakeXcom.example/video", False)]:
            route = AsyncMock()
            route.request = SimpleNamespace(url=url, resource_type="media")
            await handler._route_handler(route)
            self.assertEqual(route.abort.await_count, int(blocked))
            self.assertEqual(route.continue_.await_count, int(not blocked))

    async def test_blank_stream_pages_not_closed_as_ads(self):
        handler = AdHandler({})
        page = AsyncMock(); page.url = "about:blank"
        self.assertFalse(await handler.handle_new_page_popup(page))
        page.close.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
