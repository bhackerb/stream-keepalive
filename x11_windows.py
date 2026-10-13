"""Firefox window placement on an EWMH-compatible Linux X11 desktop."""
import asyncio
import uuid


def find_window(marker):
    from Xlib.display import Display
    from Xlib import X
    display = Display()
    try:
        root = display.screen().root
        clients = root.get_full_property(display.intern_atom("_NET_CLIENT_LIST"), X.AnyPropertyType)
        if clients is None:
            raise ValueError("Firefox tiling requires an EWMH-compatible X11 window manager")
        matches = []
        for wid in clients.value:
            window = display.create_resource_object("window", int(wid))
            title = window.get_full_property(display.intern_atom("_NET_WM_NAME"), X.AnyPropertyType)
            if title is not None and marker in bytes(title.value).decode("utf-8", errors="replace"):
                matches.append(int(wid))
        return matches[0] if len(matches) == 1 else None
    finally:
        display.close()


async def window_for_page(page):
    marker = "streamkeeper-" + uuid.uuid4().hex
    title = await page.title()
    await page.evaluate("marker => document.title = marker", marker)
    try:
        for _ in range(40):
            wid = await asyncio.to_thread(find_window, marker)
            if wid is not None:
                return wid
            await asyncio.sleep(0.05)
        raise ValueError("Could not identify a separate Firefox window for this stream")
    finally:
        await page.evaluate("title => document.title = title", title)


def move_window(wid, bounds):
    from Xlib.display import Display
    from Xlib import X, protocol
    display = Display()
    try:
        root = display.screen().root
        window = display.create_resource_object("window", wid)
        def send(name, data):
            event = protocol.event.ClientMessage(window=window, client_type=display.intern_atom(name), data=(32, data))
            root.send_event(event, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
        send("_NET_WM_STATE", [0, display.intern_atom("_NET_WM_STATE_MAXIMIZED_VERT"), display.intern_atom("_NET_WM_STATE_MAXIMIZED_HORZ"), 2, 0])
        # Gravity 0, all four dimensions supplied, source application 2.
        send("_NET_MOVERESIZE_WINDOW", [(15 << 8) | (2 << 12), bounds["x"] & 0xffffffff,
             bounds["y"] & 0xffffffff, bounds["width"], bounds["height"]])
        display.sync()
    finally:
        display.close()
