import os
from pathlib import Path
import sys
from threading import Thread
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[3]
DIST = ROOT / "web" / "dist"
sys.path.insert(0, str(ROOT))

from warden_drydock.hosted.http.application import SliceApplication, SyntheticProvider
from warden_drydock.hosted.operations.server import Handler


class BrowserTestHandler(Handler):
    def do_POST(self):
        if urlsplit(self.path).path != "/__test_shutdown__":
            return super().do_POST()
        self.send_response(204)
        self.end_headers()
        Thread(target=self.server.shutdown, daemon=True).start()


if __name__ == "__main__":
    from http.server import ThreadingHTTPServer

    os.environ["DRYDOCK_ALLOWED_HOSTS"] = "127.0.0.1:4173"
    os.environ["DRYDOCK_STATIC"] = str(DIST)
    BrowserTestHandler.application = SliceApplication(provider=SyntheticProvider())
    BrowserTestHandler.csrf_secret = "a" * 64
    ThreadingHTTPServer(("127.0.0.1", 4173), BrowserTestHandler).serve_forever()
