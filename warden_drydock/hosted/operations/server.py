from __future__ import annotations

import json
import os
import pathlib
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from .health import liveness, readiness


class Handler(SimpleHTTPRequestHandler):
    server_version = "WardenDrydock"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=os.environ["DRYDOCK_STATIC"], **kwargs)

    def end_headers(self) -> None:
        self.send_header("Content-Security-Policy", "default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def _binding_allowed(self) -> bool:
        allowed = {item.strip() for item in os.environ["DRYDOCK_ALLOWED_HOSTS"].split(",")}
        if self.headers.get("Host") not in allowed:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin in {f"http://{host}" for host in allowed}

    def do_GET(self) -> None:  # noqa: N802
        if not self._binding_allowed():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if self.path in ("/health/live", "/health/ready"):
            ok = liveness() if self.path.endswith("live") else readiness()
            body = json.dumps({"status": "ok" if ok else "unavailable"}).encode()
            self.send_response(HTTPStatus.OK if ok else HTTPStatus.SERVICE_UNAVAILABLE)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api" or self.path.startswith("/api/"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        requested = pathlib.PurePosixPath(self.path.split("?", 1)[0])
        target = pathlib.Path(os.environ["DRYDOCK_STATIC"], *requested.parts[1:])
        if not target.is_file():
            self.path = "/index.html"
        super().do_GET()

    def log_message(self, format: str, *args) -> None:
        # Request targets can contain campaign content; emit no access log.
        return None


def main() -> None:
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()


if __name__ == "__main__":
    main()
