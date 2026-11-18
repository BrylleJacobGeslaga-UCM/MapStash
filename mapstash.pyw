#!/usr/bin/env python3
"""
MapStash - free collection downloader for osu!
Start the app: python mapstash.pyw   (needs: pip install pywebview)

Unofficial: not affiliated with osu!Collector or ppy.
"""

import os
import sys


def resource(path):
    """Path to a bundled file, both when run from source and from the PyInstaller exe."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, path)


def fatal(message):
    """Show an error even when there's no console (the exe is windowed)."""
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "MapStash", 0x10)
    else:
        print(message, file=sys.stderr)
    sys.exit(1)


def window_size(webview):
    """1280x800, or smaller so it fits the screen (the interface scales to any size)."""
    w, h = 1280, 800
    try:
        screen = webview.screens[0]
        scale = min(1.0, screen.width * 0.88 / w, screen.height * 0.85 / h)
        w, h = int(w * scale), int(h * scale)
    except Exception:
        pass
    return max(w, 880), max(h, 560)


def main():
    try:
        import webview
    except ImportError:
        fatal("MapStash needs pywebview.\n\nInstall it with:\n    pip install pywebview")
    import bridge

    api = bridge.Api()
    width, height = window_size(webview)
    window = webview.create_window(
        "MapStash", url=resource(os.path.join("ui", "index.html")), js_api=api,
        width=width, height=height, min_size=(880, 560), background_color="#1c1719")
    api._set_window(window)
    window.events.closing += lambda: api.stop()  # downloads resume next time

    try:
        if os.name == "nt":
            # Use Edge WebView2 only: the old Internet Explorer engine can't draw this UI.
            webview.start(gui="edgechromium", http_server=True)
        else:
            webview.start(http_server=True)
    except Exception as e:
        if os.name == "nt":
            fatal("MapStash needs Microsoft Edge WebView2, which seems to be missing.\n\n"
                  "Get it free from Microsoft:\n"
                  "https://go.microsoft.com/fwlink/p/?LinkId=2124703\n\n"
                  f"Details: {e}")
        raise


if __name__ == "__main__":
    main()
