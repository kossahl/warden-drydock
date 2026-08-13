from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit

DIST = Path(__file__).resolve().parents[2] / "dist"


class StaticFallbackHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DIST), **kwargs)

    def do_GET(self):
        request_path = urlsplit(self.path).path
        if request_path == "/healthz":
            self.send_response(204)
            self.end_headers()
            return
        if request_path == "/api" or request_path.startswith("/api/"):
            self.send_error(404)
            return
        requested = DIST / request_path.lstrip("/")
        if "text/html" in self.headers.get("Accept", "") and not requested.is_file():
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self):
        if self.path != "/__test_shutdown__":
            self.send_error(404)
            return
        self.send_response(204)
        self.end_headers()
        Thread(target=self.server.shutdown, daemon=True).start()


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 4173), StaticFallbackHandler).serve_forever()
