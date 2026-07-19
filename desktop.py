"""Desktop entry point for Panda.

System workflow: find a free localhost port, start the Flask app in a background
thread, wait until it responds, then open ``index.html`` in a pywebview window.
User workflow: launch Panda.vbs -> use the desktop window -> close the window to
stop the local server. No analysis logic belongs in this wrapper.
"""

import os
import socket
import threading
import time
from urllib.request import urlopen

import webview


HOST = "127.0.0.1"
PORT = 5127


def find_free_port(start_port=PORT):
    """Return the first available localhost port at or above ``start_port``."""
    port = start_port
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((HOST, port))
                return port
            except OSError:
                port += 1


def wait_for_server(url, timeout=15):
    """Poll the local Flask URL until it responds or ``timeout`` expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urlopen(url, timeout=1):
                return True
        except Exception:
            time.sleep(0.2)
    return False


def main():
    """Start Flask in-process and host its UI inside a native pywebview window."""
    os.environ.setdefault("PANDA_LOCAL_STORAGE", "1")
    os.environ.setdefault("PANDA_DESKTOP", "1")

    from app import app

    port = find_free_port()
    url = f"http://{HOST}:{port}"

    def run_server():
        app.run(host=HOST, port=port, debug=False, use_reloader=False)

    threading.Thread(target=run_server, daemon=True).start()
    wait_for_server(url)

    # pywebview disables browser-style file downloads by default (blob URLs,
    # <a download>, and send_file responses all rely on this) — every export
    # button (CSV, Excel, PNG, PDF) would otherwise do nothing at all inside
    # the desktop window. Must be set before webview.start().
    webview.settings["ALLOW_DOWNLOADS"] = True

    webview.create_window(
        "Panda",
        url,
        width=1440,
        height=920,
        min_size=(1100, 720),
    )
    webview.start()


if __name__ == "__main__":
    main()
