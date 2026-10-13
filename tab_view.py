"""Fill the browser tab without moving/recreating the video or entering OS fullscreen."""

EXPAND_PLAYER = r"""(target, enabled) => {
    const doc = target.ownerDocument;
    const restore = () => {
        doc.getElementById('streamkeeper-tab-style')?.remove();
        doc.querySelectorAll('[data-sk-tab]').forEach(el => el.removeAttribute('data-sk-tab'));
    };
    if (!enabled) { restore(); return true; }
    restore();
    const style = doc.createElement('style');
    style.id = 'streamkeeper-tab-style';
    style.textContent = `
      html, body { overflow:hidden!important; }
      body { visibility:hidden!important; }
      [data-sk-tab="ancestor"] { transform:none!important; filter:none!important;
        perspective:none!important; contain:none!important; overflow:visible!important;
        position:static!important; opacity:1!important; }
      [data-sk-tab="player"] { position:fixed!important; inset:0!important;
        width:100vw!important; height:100vh!important; max-width:none!important;
        max-height:none!important; margin:0!important; padding:0!important;
        border:0!important; z-index:2147483646!important; visibility:visible!important;
        background:#000!important; object-fit:contain!important; }
      [data-sk-tab="player"] * { visibility:visible!important; }
      [data-sk-tab="player"] video { width:100%!important; height:100%!important; object-fit:contain!important; }
    `;
    doc.head.appendChild(style);
    for (let el = target.parentElement; el; el = el.parentElement) el.dataset.skTab = 'ancestor';
    target.dataset.skTab = 'player';
    if (!doc.defaultView.__skTabEscape) {
        doc.defaultView.__skTabEscape = true;
        doc.addEventListener('keydown', ev => {
            if (ev.key === 'Escape') {
                restore(); doc.documentElement.dataset.skTabDisabled = 'true';
                if (window.parent !== window) window.parent.postMessage('streamkeeper-restore-tab', '*');
                for (const frame of doc.querySelectorAll('iframe')) frame.contentWindow?.postMessage('streamkeeper-restore-tab', '*');
            }
        }, true);
        window.addEventListener('message', ev => {
            if (ev.data !== 'streamkeeper-restore-tab') return;
            const children = Array.from(doc.querySelectorAll('iframe')).map(f => f.contentWindow);
            if (ev.source !== window.parent && !children.includes(ev.source)) return;
            restore(); doc.documentElement.dataset.skTabDisabled = 'true';
            if (window.parent !== window && ev.source !== window.parent) window.parent.postMessage(ev.data, '*');
            for (const child of children) if (child !== ev.source) child?.postMessage(ev.data, '*');
        });
    }
    return true;
}"""


async def fill_tab(page, enabled=True, automatic=False):
    if automatic and await page.evaluate("() => document.documentElement.dataset.skTabDisabled === 'true'"):
        return False
    if not enabled:
        for frame in page.frames:
            await frame.evaluate("() => { document.getElementById('streamkeeper-tab-style')?.remove(); document.querySelectorAll('[data-sk-tab]').forEach(el => el.removeAttribute('data-sk-tab')); document.documentElement.dataset.skTabDisabled = 'true'; }")
        return True
    for frame in page.frames:
        try:
            video = await frame.query_selector("video")
            if not video:
                continue
            if automatic and await frame.evaluate("() => document.documentElement.dataset.skTabDisabled === 'true'"):
                return False
            await frame.evaluate("() => delete document.documentElement.dataset.skTabDisabled")
            # Preserve known player controls alongside the video where possible.
            target = await video.evaluate_handle("v => v.closest('.jwplayer, .video-js, .plyr, [data-player]') || v")
            await target.evaluate(EXPAND_PLAYER, True)
            await target.dispose()
            await video.dispose()
            current = frame
            while current.parent_frame:
                element = await current.frame_element()
                await element.evaluate(EXPAND_PLAYER, True)
                await element.dispose()
                current = current.parent_frame
            await page.evaluate("() => delete document.documentElement.dataset.skTabDisabled")
            return True
        except Exception:
            continue
    return False
