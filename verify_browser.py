"""Explicit browser smoke, using synthetic pages only: python3 verify_browser.py firefox."""
import asyncio
import json
import sys
import tempfile

from stream_keeper import StreamKeeper
from tab_view import fill_tab


async def main(engine):
    with tempfile.TemporaryDirectory(prefix="streamkeeper-smoke-") as profile:
        keeper = StreamKeeper({"browser": {"engine": engine, "headless": True,
            "user_data_dir": profile, "firefox_user_data_dir": profile},
            "keepalive": {"auto_scan_interval_seconds": 3600}})
        try:
            await keeper.start_browser()
            assert keeper.ad_handler.network_verified, "installed route did not block the canary"
            async def fixture(route):
                if route.request.url == "https://fixture.invalid/":
                    await route.fulfill(content_type="text/html", body='<h1>Game directory</h1><div style="transform:translateX(0);overflow:hidden;width:500px"><iframe src="https://frame.invalid/player" width="400" height="240"></iframe></div>')
                elif route.request.url == "https://frame.invalid/player":
                    await route.fulfill(content_type="text/html", body='<div class="jwplayer" style="width:320px;height:180px"><video controls style="width:320px;height:180px"></video><button>Player controls</button></div>')
                else:
                    await route.abort()
            await keeper.context.route("**/*", fixture)
            page = await keeper.context.new_page()
            await page.set_viewport_size({"width": 1280, "height": 720})
            await page.goto("https://fixture.invalid/")
            frame = page.frames[1]
            assert await fill_tab(page)
            await keeper.ad_handler.dismiss_overlays(page)
            box = await page.locator("iframe").bounding_box()
            assert all(abs(box[k] - v) <= 1 for k, v in {"x":0,"y":0,"width":1280,"height":720}.items()), box
            assert await frame.locator("video").count() == 1
            assert await frame.locator("button").is_visible()
            await page.screenshot(path=f"/tmp/streamwatcher-{engine}-tab.png")
            await page.set_viewport_size({"width": 640, "height": 360})
            box = await page.locator("iframe").bounding_box()
            assert abs(box["width"] - 640) <= 1 and abs(box["height"] - 360) <= 1, box
            await frame.evaluate("() => document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', bubbles:true}))")
            await page.wait_for_function("() => !document.getElementById('streamkeeper-tab-style')")
            assert not await fill_tab(page, automatic=True), "Escape must persist until manually re-enabled"
            assert await fill_tab(page), "manual re-enable failed"
            assert await fill_tab(page, enabled=False)
            assert await page.locator("#streamkeeper-tab-style").count() == 0
            print(json.dumps({"engine":engine, "adblock_canary":True, "nested_iframe_fill":True,
                              "resize":True, "escape_restore":True, "manual_restore":True}))
        finally:
            await keeper.shutdown()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "firefox"))
