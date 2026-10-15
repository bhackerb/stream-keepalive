"""Firefox window placement on an EWMH-compatible Linux X11 desktop."""
import asyncio
import uuid
import time


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


def window_matches(wid, bounds, fullscreen=False):
    """Read actual client geometry/state; do not move or activate the window."""
    from Xlib.display import Display
    from Xlib import X
    display = Display()
    try:
        root = display.screen().root
        window = display.create_resource_object('window', wid)
        geometry = window.get_geometry()
        position = root.translate_coords(window, 0, 0)
        state = window.get_full_property(display.intern_atom('_NET_WM_STATE'), X.AnyPropertyType)
        focused = state is not None and display.intern_atom('_NET_WM_STATE_FULLSCREEN') in state.value
        return focused == fullscreen and all(abs(a-b) <= 2 for a,b in zip(
            (position.x, position.y, geometry.width, geometry.height),
            (bounds['x'], bounds['y'], bounds['width'], bounds['height'])))
    finally:
        display.close()


def wait_window_matches(wid, bounds, fullscreen=False):
    """Allow the window manager one second to settle asynchronous geometry."""
    for _ in range(50):
        if window_matches(wid,bounds,fullscreen):return True
        time.sleep(.02)
    return False


def move_window(wid, bounds, fullscreen=None):
    from Xlib.display import Display
    from Xlib import X, Xatom, protocol
    display = Display()
    try:
        root = display.screen().root
        window = display.create_resource_object("window", wid)
        def send(name, data):
            event = protocol.event.ClientMessage(window=window, client_type=display.intern_atom(name), data=(32, data))
            root.send_event(event, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
        def wait_fullscreen(enabled):
            display.sync()
            for _ in range(50):
                current = window.get_full_property(display.intern_atom("_NET_WM_STATE"), X.AnyPropertyType)
                actual = current is not None and display.intern_atom("_NET_WM_STATE_FULLSCREEN") in current.value
                if actual == enabled:
                    return
                time.sleep(0.02)
            raise ValueError("Window manager did not confirm TV fullscreen transition")
        state = window.get_full_property(display.intern_atom("_NET_WM_STATE"), X.AnyPropertyType)
        is_fullscreen = state is not None and display.intern_atom("_NET_WM_STATE_FULLSCREEN") in state.value
        saved_type = display.intern_atom("_STREAMKEEPER_FOCUS_WINDOW_TYPE")
        saved_hints = display.intern_atom("_STREAMKEEPER_FOCUS_MOTIF_HINTS")
        window_type = display.intern_atom("_NET_WM_WINDOW_TYPE")
        motif = display.intern_atom("_MOTIF_WM_HINTS")
        original = window.get_full_property(saved_type, X.AnyPropertyType)
        if fullscreen and original is None:
            # Firefox companion popups are DIALOG windows. Muffin refuses their
            # fullscreen requests until they become normal, undecorated windows.
            old_type = window.get_full_property(window_type, X.AnyPropertyType)
            old_hints = window.get_full_property(motif, X.AnyPropertyType)
            window.change_property(saved_type, Xatom.ATOM, 32, list(old_type.value) if old_type is not None else [])
            window.change_property(saved_hints, motif, 32, list(old_hints.value) if old_hints is not None else [])
            window.change_property(window_type, Xatom.ATOM, 32, [display.intern_atom("_NET_WM_WINDOW_TYPE_NORMAL")])
            window.change_property(motif, motif, 32, [2, 0, 0, 0, 0])
            display.sync()
            time.sleep(0.1)  # Let the WM process type/decorations before fullscreen.
        if fullscreen is True and is_fullscreen and window_matches(wid,bounds,fullscreen=True):
            return
        if fullscreen is not None and is_fullscreen:
            # Explicit add/remove is idempotent; never toggle a user's other windows.
            send("_NET_WM_STATE", [0, display.intern_atom("_NET_WM_STATE_FULLSCREEN"), 0, 2, 0])
            wait_fullscreen(False)
        if fullscreen is False and original is not None:
            for target, saved in ((window_type, saved_type), (motif, saved_hints)):
                old = window.get_full_property(saved, X.AnyPropertyType)
                if old is not None and len(old.value):
                    window.change_property(target, Xatom.ATOM if target == window_type else motif, 32, list(old.value))
                else:
                    window.delete_property(target)
                window.delete_property(saved)
            display.sync()
            time.sleep(0.1)
        send("_NET_WM_STATE", [0, display.intern_atom("_NET_WM_STATE_MAXIMIZED_VERT"), display.intern_atom("_NET_WM_STATE_MAXIMIZED_HORZ"), 2, 0])
        # Gravity 0, all four dimensions supplied, source application 2.
        send("_NET_MOVERESIZE_WINDOW", [(15 << 8) | (2 << 12), bounds["x"] & 0xffffffff,
             bounds["y"] & 0xffffffff, bounds["width"], bounds["height"]])
        if fullscreen:
            send("_NET_WM_STATE", [1, display.intern_atom("_NET_WM_STATE_FULLSCREEN"), 0, 2, 0])
            wait_fullscreen(True)
        display.sync()
    finally:
        display.close()
