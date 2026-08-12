from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

DIST = Path(__file__).resolve().parents[2] / "dist"


class StaticFallbackHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DIST), **kwargs)

    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(204)
            self.end_headers()
            return
        if self.path.startswith("/api/"):
            self.send_error(404)
            return
        requested = DIST / self.path.lstrip("/").split("?", 1)[0]
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
