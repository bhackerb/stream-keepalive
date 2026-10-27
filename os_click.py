"""Trusted X11 pointer click for autoplay steps that page scripts cannot perform.

Synthetic DOM clicks are not user gestures, and cross-origin embeds cannot be reached
from the companion. The server sends one XTEST press/release at absolute screen
coordinates it has already validated against connected displays. python-xlib is
imported lazily so hosts without X11 can still import the regular-browser module.
"""
import os
import time


def click_point(x, y):
    from Xlib import X, display
    from Xlib.ext import xtest
    d = display.Display(os.environ.get('DISPLAY') or ':0')
    try:
        xtest.fake_input(d, X.MotionNotify, x=int(x), y=int(y))
        d.sync()
        time.sleep(.3)
        xtest.fake_input(d, X.ButtonPress, 1)
        d.sync()
        time.sleep(.08)
        xtest.fake_input(d, X.ButtonRelease, 1)
        d.sync()
    finally:
        d.close()
