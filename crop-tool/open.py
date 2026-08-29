#!/usr/bin/env python3
"""Serve photo-crop with one image preloaded and open it in the browser.

usage: open.py IMAGE_PATH [PORT]

Serves index.html at / and the image at /scan<ext>, then opens
http://127.0.0.1:<port>/?file=scan<ext> in the default browser (or in the
binary named by the PHOTO_CROP_BROWSER env var).
The server stops itself a few seconds after the page has fetched the
image, so it can run in the foreground and still return the terminal.
Stdlib only. Port 0 (default) picks a free port.
"""
import http.server
import mimetypes
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

GRACE = 5.0
CAP = 600.0


def main():
    if len(sys.argv) < 2:
        print(f"usage: {sys.argv[0]} IMAGE_PATH [PORT]")
        sys.exit(1)
    image = Path(sys.argv[1]).resolve()
    if not image.is_file():
        print(f"not a file: {image}")
        sys.exit(1)
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    root = Path(__file__).resolve().parent
    name = "scan" + image.suffix.lower()
    ctype = mimetypes.guess_type(image.name)[0] or "application/octet-stream"

    activity = {"last": time.time(), "image_served": False}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            activity["last"] = time.time()
            if path in ("/", "/index.html"):
                data = (root / "index.html").read_bytes()
                ctype_ = "text/html; charset=utf-8"
            elif path == "/" + name:
                data = image.read_bytes()
                ctype_ = ctype
                activity["image_served"] = True
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype_)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    class Server(http.server.ThreadingHTTPServer):
        daemon_threads = True

    server = Server(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/?file={name}"
    browser = os.environ.get("PHOTO_CROP_BROWSER")
    if browser:
        threading.Timer(0.3, lambda: subprocess.Popen([browser, url])).start()
    else:
        threading.Timer(0.3, webbrowser.open, [url]).start()
    print(f"photo-crop: {url}  (server stops once the page has loaded)", flush=True)

    def watchdog():
        while True:
            time.sleep(1)
            now = time.time()
            if (activity["image_served"] and now - activity["last"] > GRACE) or now - activity["last"] > CAP:
                break
        server.shutdown()

    threading.Thread(target=watchdog, daemon=True).start()
    server.serve_forever()
    print("photo-crop: done.", flush=True)


if __name__ == "__main__":
    main()
