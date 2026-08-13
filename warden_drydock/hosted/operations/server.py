from __future__ import annotations

import json
import os
import pathlib
import urllib.parse
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

    def _prepare_request(self) -> bool:
        if self._binding_allowed():
            return True
        self.send_error(HTTPStatus.FORBIDDEN)
        return False

    def do_GET(self) -> None:  # noqa: N802
        if not self._prepare_request():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/health/live", "/health/ready"):
            ok = liveness() if path.endswith("live") else readiness()
            body = json.dumps({"status": "ok" if ok else "unavailable"}).encode()
            self.send_response(HTTPStatus.OK if ok else HTTPStatus.SERVICE_UNAVAILABLE)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api" or path.startswith("/api/"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        requested = pathlib.PurePosixPath(path)
        target = pathlib.Path(os.environ["DRYDOCK_STATIC"], *requested.parts[1:])
        if not target.is_file():
            self.path = "/index.html"
        super().do_GET()

    def do_HEAD(self) -> None:  # noqa: N802
        if not self._prepare_request():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api" or path.startswith("/api/"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.path = path
        super().do_HEAD()

    def _reject_unsupported(self) -> None:
        if self._prepare_request():
            self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)

    do_POST = _reject_unsupported
    do_PUT = _reject_unsupported
    do_PATCH = _reject_unsupported
    do_DELETE = _reject_unsupported
    do_OPTIONS = _reject_unsupported

    def log_message(self, format: str, *args) -> None:
        # Request targets can contain campaign content; emit no access log.
        return None


def main() -> None:
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()


if __name__ == "__main__":
    main()
