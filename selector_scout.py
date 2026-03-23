#!/usr/bin/env python3
"""
Selector Scout — Helper tool to discover and test CSS selectors on streaming sites.

Run this against a live site to figure out the correct selectors for:
- Game listings
- Team names
- Stream source links
- Video players
- Ad overlays

Usage:
    python selector_scout.py <url>
    python selector_scout.py https://streamed.pk/category/ice-hockey
    python selector_scout.py https://www.onhockey.tv

This opens the page in a browser and runs analysis to find interactive elements.
"""

import asyncio
import json
import sys

from playwright.async_api import async_playwright


ANALYSIS_JS = """() => {
    const results = {
        videos: [],
        iframes: [],
        links_with_team_text: [],
        clickable_cards: [],
        overlays: [],
        close_buttons: [],
        stream_links: [],
    };

    // Find video elements
    document.querySelectorAll('video').forEach(v => {
        results.videos.push({
            src: v.src || v.currentSrc || '(no src)',
            width: v.videoWidth,
            height: v.videoHeight,
            readyState: v.readyState,
            paused: v.paused,
            selector: getSelector(v),
        });
    });

    // Find iframes (potential stream embeds)
    document.querySelectorAll('iframe').forEach(f => {
        results.iframes.push({
            src: f.src || '(no src)',
            width: f.width,
            height: f.height,
            selector: getSelector(f),
        });
    });

    // Find links that might be game cards
    document.querySelectorAll('a').forEach(a => {
        const text = (a.innerText || '').trim();
        // Look for links with team-like names (capitalized words, vs/at patterns)
        if (text.length > 5 && text.length < 200 &&
            (text.match(/vs\\.?|at |@/i) || text.match(/[A-Z][a-z]+ [A-Z][a-z]+/))) {
            results.links_with_team_text.push({
                text: text.substring(0, 100),
                href: a.href,
                selector: getSelector(a),
                classes: a.className,
            });
        }
        // Stream source links
        if (a.href && (a.href.includes('stream') || a.href.includes('source') ||
            a.href.includes('link') || a.href.includes('np_'))) {
            results.stream_links.push({
                text: text.substring(0, 50),
                href: a.href,
                selector: getSelector(a),
            });
        }
    });

    // Find potential overlay/popup elements
    document.querySelectorAll('div, section').forEach(el => {
        const style = window.getComputedStyle(el);
        if ((style.position === 'fixed' || style.position === 'absolute') &&
            parseFloat(style.zIndex) > 100 &&
            el.offsetWidth > 200 && el.offsetHeight > 200) {
            results.overlays.push({
                tag: el.tagName,
                classes: el.className.substring(0, 80),
                id: el.id,
                size: `${el.offsetWidth}x${el.offsetHeight}`,
                zIndex: style.zIndex,
                selector: getSelector(el),
            });
        }
    });

    // Find close/dismiss buttons
    document.querySelectorAll('button, [role="button"], .close, [class*="close"]').forEach(btn => {
        const text = (btn.innerText || btn.textContent || '').trim();
        if (text.length < 30 && (
            text.match(/close|dismiss|x|✕|✖|×/i) ||
            btn.className.match(/close/i) ||
            btn.getAttribute('aria-label')?.match(/close/i)
        )) {
            results.close_buttons.push({
                text: text || '(no text)',
                selector: getSelector(btn),
                classes: btn.className,
            });
        }
    });

    // Helper: generate a unique-ish CSS selector for an element
    function getSelector(el) {
        if (el.id) return '#' + el.id;
        let path = [];
        while (el && el.nodeType === 1) {
            let selector = el.tagName.toLowerCase();
            if (el.id) {
                path.unshift('#' + el.id);
                break;
            }
            if (el.className && typeof el.className === 'string') {
                const classes = el.className.trim().split(/\\s+/).slice(0, 2).join('.');
                if (classes) selector += '.' + classes;
            }
            path.unshift(selector);
            el = el.parentElement;
            if (path.length > 4) break;
        }
        return path.join(' > ');
    }

    return results;
}"""


async def scout(url: str):
    print(f"🔍 Selector Scout — Analyzing: {url}")
    print("=" * 60)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page(viewport={"width": 1920, "height": 1080})

        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)  # Let dynamic content load

        results = await page.evaluate(ANALYSIS_JS)

        # Also check inside iframes
        for frame in page.frames:
            if frame != page.main_frame:
                try:
                    iframe_results = await frame.evaluate(ANALYSIS_JS)
                    for key in results:
                        if iframe_results.get(key):
                            for item in iframe_results[key]:
                                item['_in_iframe'] = frame.url[:80]
                            results[key].extend(iframe_results[key])
                except Exception:
                    pass

        # Pretty print results
        for category, items in results.items():
            if items:
                print(f"\n📌 {category.upper()} ({len(items)} found):")
                print("-" * 40)
                for item in items[:10]:  # Limit output
                    print(f"  {json.dumps(item, indent=4, default=str)}")

        print("\n" + "=" * 60)
        print("💡 Use these selectors in config.yaml under sites.<site>.selectors")
        print("   The browser is still open — inspect elements manually too!")
        print("   Press Enter to close...")

        input()
        await browser.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python selector_scout.py <url>")
        print("  e.g. python selector_scout.py https://streamed.pk/category/ice-hockey")
        sys.exit(1)

    asyncio.run(scout(sys.argv[1]))
