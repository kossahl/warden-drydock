from __future__ import annotations

import hmac
from http.cookies import SimpleCookie
import json
import os
import pathlib
import re
import secrets
import threading
import urllib.parse
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from .health import liveness, readiness
from warden_drydock.hosted.http.application import HTTPFailure, SliceApplication, SyntheticProvider
from warden_drydock.hosted.http.repository import PostgresHTTPRepository
from warden_drydock.hosted.proposals import PostgresProposalRepository
from warden_drydock.hosted.ai.repository import PostgresAIRepository
from warden_drydock.hosted.projections import PostgresAtlasProjectionRepository
from warden_drydock.hosted.revisions import PostgresWorkflowRepository
from warden_drydock.hosted.http.query import parse_flat_query, require_int


_ROUTES = {
    "revision": re.compile(r"^/api/v1/campaigns/([^/]+)/revisions/([^/]+)$"),
    "record": re.compile(r"^/api/v1/campaigns/([^/]+)/revisions/([^/]+)/records/([^/]+)$"),
    "generation_start": re.compile(r"^/api/v1/campaigns/([^/]+)/revisions/([^/]+)/generations$"),
    "events": re.compile(r"^/api/v1/generations/([^/]+)/events$"),
    "generation": re.compile(r"^/api/v1/generations/([^/]+)$"),
    "proposal_create": re.compile(r"^/api/v1/generations/([^/]+)/proposals$"),
    "proposal": re.compile(r"^/api/v1/proposals/([^/]+)/versions/(\d+)$"),
    "correct": re.compile(r"^/api/v1/proposals/([^/]+)/versions/(\d+)/corrections$"),
    "reject": re.compile(r"^/api/v1/proposals/([^/]+)/versions/(\d+)/rejection$"),
    "approve": re.compile(r"^/api/v1/proposals/([^/]+)/versions/(\d+)/approval$"),
    "atlas_overview": re.compile(r"^/api/v1/campaigns/([^/]+)/atlas/overview$"),
    "atlas_records": re.compile(r"^/api/v1/campaigns/([^/]+)/atlas/records$"),
    "atlas_detail": re.compile(r"^/api/v1/campaigns/([^/]+)/atlas/records/([^/]+)$"),
    "atlas_neighborhood": re.compile(r"^/api/v1/campaigns/([^/]+)/atlas/records/([^/]+)/neighborhood$"),
    "atlas_history": re.compile(r"^/api/v1/campaigns/([^/]+)/atlas/history$"),
    "atlas_workflow": re.compile(r"^/api/v1/campaigns/([^/]+)/atlas/workflow-summary$"),
    "atlas_generations": re.compile(r"^/api/v1/campaigns/([^/]+)/atlas/generations$"),
    "atlas_proposals": re.compile(r"^/api/v1/campaigns/([^/]+)/atlas/proposals$"),
}


class Handler(SimpleHTTPRequestHandler):
    server_version = "WardenDrydock"
    application: SliceApplication | None = None
    csrf_secret: str | None = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=os.environ["DRYDOCK_STATIC"], **kwargs)

    def parse_request(self) -> bool:
        self._binding_verified = False
        if not super().parse_request():
            return False
        if self._binding_allowed():
            self._binding_verified = True
            return True
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/api/v1/"):
            self._send_json(
                HTTPStatus.FORBIDDEN,
                HTTPFailure(
                    403, "unsafe_binding", "request_binding_rejected", "request_integrity",
                    request_id="request_binding",
                ).payload,
            )
        else:
            self.send_error(HTTPStatus.FORBIDDEN)
        return False

    def end_headers(self) -> None:
        self.send_header("Content-Security-Policy", "default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if getattr(self, "_binding_verified", False) and self.command in {"GET", "HEAD"} and self.csrf_secret is not None:
            self.send_header("X-CSRF-Token", self.csrf_secret)
            self.send_header("Set-Cookie", f"drydock_csrf={self.csrf_secret}; Path=/; SameSite=Strict; HttpOnly")
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

    def _csrf_allowed(self) -> bool:
        expected = self.csrf_secret
        supplied = self.headers.get("X-CSRF-Token")
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return False
        stored = cookie.get("drydock_csrf")
        return bool(
            expected and supplied and stored
            and hmac.compare_digest(supplied, expected)
            and hmac.compare_digest(stored.value, expected)
        )

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
            if self.command != "HEAD":
                self.wfile.write(body)
            return
        if path.startswith("/api/v1/"):
            self._api_get(path)
            return
        if path == "/api" or path.startswith("/api/"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        requested = pathlib.PurePosixPath(path)
        target = pathlib.Path(os.environ["DRYDOCK_STATIC"], *requested.parts[1:])
        if not target.is_file():
            accepted = self.headers.get("Accept", "")
            if "text/html" not in accepted:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
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

    def do_POST(self) -> None:  # noqa: N802
        if not self._prepare_request():
            return
        if not self._csrf_allowed():
            self._send_json(
                HTTPStatus.FORBIDDEN,
                HTTPFailure(
                    403, "unsafe_binding", "csrf_binding_rejected", "request_integrity",
                    request_id="request_csrf",
                ).payload,
            )
            return
        path = urllib.parse.urlsplit(self.path).path
        if not path.startswith("/api/v1/"):
            self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)
            return
        self._api_post(path)

    def _application(self) -> SliceApplication:
        if self.application is None:
            raise HTTPFailure(503, "service_unavailable", "application_unavailable", "http")
        return self.application

    def _json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise HTTPFailure(422, "unsafe_binding", "invalid_content_length", "request_parse") from exc
        if length < 2 or length > 1_000_000 or self.headers.get_content_type() != "application/json":
            raise HTTPFailure(422, "unsafe_binding", "invalid_json_request", "request_parse")
        try:
            value = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPFailure(422, "unsafe_binding", "invalid_json_request", "request_parse") from exc
        if not isinstance(value, dict):
            raise HTTPFailure(422, "unsafe_binding", "invalid_json_request", "request_parse")
        return value

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return

    def _api_get(self, path: str) -> None:
        try:
            app = self._application()
            raw_query = urllib.parse.urlsplit(self.path).query
            if path == "/api/v1/provider/readiness":
                parse_flat_query(raw_query, singleton=frozenset())
                status, payload = app.provider_readiness()
            elif path == "/api/v1/campaigns":
                parse_flat_query(raw_query, singleton=frozenset())
                status, payload = app.campaign_collection()
            elif match := _ROUTES["revision"].fullmatch(path):
                parse_flat_query(raw_query, singleton=frozenset())
                status, payload = app.revision_view(*match.groups())
            elif match := _ROUTES["record"].fullmatch(path):
                parse_flat_query(raw_query, singleton=frozenset())
                status, payload = app.record_view(*match.groups())
            elif match := _ROUTES["generation"].fullmatch(path):
                parse_flat_query(raw_query, singleton=frozenset())
                status, payload = app.generation_view(match.group(1))
            elif match := _ROUTES["proposal"].fullmatch(path):
                parse_flat_query(raw_query, singleton=frozenset())
                status, payload = app.proposal_view(match.group(1), int(match.group(2)))
            elif match := _ROUTES["events"].fullmatch(path):
                self._send_events(app, match.group(1))
                return
            elif match := _ROUTES["atlas_overview"].fullmatch(path):
                query = self._atlas_binding_query(raw_query)
                status, payload = app.atlas_overview(match.group(1), **query)
            elif match := _ROUTES["atlas_records"].fullmatch(path):
                query = parse_flat_query(
                    raw_query,
                    singleton=frozenset({"revision_id", "revision_ordinal", "tree_digest", "q", "limit", "cursor"}),
                    repeated=frozenset({"type", "authority", "status"}),
                    required=frozenset({"revision_id", "revision_ordinal", "tree_digest", "limit"}),
                )
                status, payload = app.atlas_record_library(
                    match.group(1), str(query["revision_id"]),
                    require_int(query["revision_ordinal"], minimum=1, maximum=2_147_483_647),
                    str(query["tree_digest"]), query=str(query.get("q", "")),
                    record_types=tuple(query.get("type", ())),
                    authorities=tuple(query.get("authority", ())),
                    statuses=tuple(query.get("status", ())),
                    limit=require_int(query["limit"], minimum=1, maximum=100),
                    cursor=str(query["cursor"]) if "cursor" in query else None,
                )
            elif match := _ROUTES["atlas_neighborhood"].fullmatch(path):
                query = parse_flat_query(
                    raw_query,
                    singleton=frozenset({"revision_id", "revision_ordinal", "tree_digest", "depth", "limit", "cursor"}),
                    required=frozenset({"revision_id", "revision_ordinal", "tree_digest", "depth", "limit"}),
                )
                status, payload = app.atlas_neighborhood(
                    match.group(1), match.group(2), str(query["revision_id"]),
                    require_int(query["revision_ordinal"], minimum=1, maximum=2_147_483_647),
                    str(query["tree_digest"]),
                    depth=require_int(query["depth"], minimum=1, maximum=1),
                    limit=require_int(query["limit"], minimum=1, maximum=100),
                    cursor=str(query["cursor"]) if "cursor" in query else None,
                )
            elif match := _ROUTES["atlas_detail"].fullmatch(path):
                query = self._atlas_binding_query(raw_query)
                status, payload = app.atlas_record_detail(match.group(1), match.group(2), **query)
            elif match := _ROUTES["atlas_history"].fullmatch(path):
                query = parse_flat_query(
                    raw_query,
                    singleton=frozenset({"revision_id", "revision_ordinal", "tree_digest", "subject_record_id", "limit", "cursor", "direction"}),
                    required=frozenset({"revision_id", "revision_ordinal", "tree_digest", "limit"}),
                )
                direction = str(query.get("direction", "forward"))
                if direction not in {"forward", "backward"}:
                    raise ValueError("invalid_query_binding")
                status, payload = app.atlas_history(
                    match.group(1), str(query["revision_id"]),
                    require_int(query["revision_ordinal"], minimum=1, maximum=2_147_483_647),
                    str(query["tree_digest"]),
                    subject_record_id=str(query["subject_record_id"]) if "subject_record_id" in query else None,
                    limit=require_int(query["limit"], minimum=1, maximum=100),
                    cursor=str(query["cursor"]) if "cursor" in query else None,
                    direction=direction,
                )
            elif match := _ROUTES["atlas_workflow"].fullmatch(path):
                query = self._atlas_binding_query(raw_query)
                status, payload = app.atlas_workflow_summary(match.group(1), **query)
            elif match := _ROUTES["atlas_generations"].fullmatch(path):
                query = self._atlas_generation_collection_query(raw_query)
                status, payload = app.atlas_generation_collection(
                    match.group(1), **query,
                )
            elif match := _ROUTES["atlas_proposals"].fullmatch(path):
                query = self._atlas_proposal_collection_query(raw_query)
                status, payload = app.atlas_proposal_collection(
                    match.group(1), **query,
                )
            else:
                raise HTTPFailure(404, "not_found", "route_not_found", "routing")
            self._send_json(status, payload)
        except HTTPFailure as exc:
            self._send_json(exc.status, exc.payload)
        except (KeyError, TypeError, ValueError):
            self._send_json(422, HTTPFailure(422, "unsafe_binding", "invalid_query_binding", "request_validation").payload)
        except Exception:
            self._send_json(503, HTTPFailure(503, "service_unavailable", "service_unavailable", "request_dispatch", retryable=True).payload)

    def _send_events(self, app: SliceApplication, generation_id: str) -> None:
        query = parse_flat_query(
            urllib.parse.urlsplit(self.path).query,
            singleton=frozenset({"after"}),
        )
        try:
            after = require_int(query["after"], minimum=0, maximum=2_147_483_647) if "after" in query else None
            last = int(self.headers["Last-Event-ID"]) if self.headers.get("Last-Event-ID") is not None else None
        except (ValueError, IndexError):
            raise HTTPFailure(422, "unsafe_binding", "resume_sequence_invalid", "ask_resume")
        status, events = app.generation_events(generation_id, after=after, last_event_id=last)
        body = b"".join(
            f"id: {event['sequence']}\nevent: {event['event_type']}\ndata: {json.dumps(event, separators=(',', ':'), sort_keys=True)}\n\n".encode("utf-8")
            for event in events
        )
        self.send_response(status)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return

    def _api_post(self, path: str) -> None:
        try:
            app, payload = self._application(), self._json_body()
            dispatch = None
            if path == "/api/v1/provider/consent":
                status, response = app.provider_consent(payload)
            elif path == "/api/v1/campaigns":
                status, response = app.create_campaign(payload)
            elif match := _ROUTES["generation_start"].fullmatch(path):
                status, response, reserved = app.start_generation(*match.groups(), payload)
                dispatch = response["generation_id"] if reserved else None
            elif match := _ROUTES["proposal_create"].fullmatch(path):
                status, response = app.create_proposal(match.group(1), payload)
            elif match := _ROUTES["correct"].fullmatch(path):
                status, response = app.correct_proposal(match.group(1), int(match.group(2)), payload)
            elif match := _ROUTES["reject"].fullmatch(path):
                status, response = app.reject_proposal(match.group(1), int(match.group(2)), payload)
            elif match := _ROUTES["approve"].fullmatch(path):
                status, response = app.approve_proposal(match.group(1), int(match.group(2)), payload)
            else:
                raise HTTPFailure(404, "not_found", "route_not_found", "routing")
            self._send_json(status, response)
            if dispatch is not None:
                threading.Thread(target=app.dispatch_generation, args=(dispatch,), daemon=True).start()
        except HTTPFailure as exc:
            self._send_json(exc.status, exc.payload)
        except (KeyError, TypeError, ValueError):
            self._send_json(422, HTTPFailure(422, "unsafe_binding", "invalid_request", "request_validation").payload)
        except Exception:
            self._send_json(503, HTTPFailure(503, "service_unavailable", "service_unavailable", "request_dispatch", retryable=True).payload)

    @staticmethod
    def _atlas_binding_query(raw_query: str) -> dict[str, object]:
        query = parse_flat_query(
            raw_query,
            singleton=frozenset({"revision_id", "revision_ordinal", "tree_digest"}),
            required=frozenset({"revision_id", "revision_ordinal", "tree_digest"}),
        )
        return {
            "revision_id": str(query["revision_id"]),
            "ordinal": require_int(query["revision_ordinal"], minimum=1, maximum=2_147_483_647),
            "tree_digest": str(query["tree_digest"]),
        }

    @staticmethod
    def _atlas_generation_collection_query(raw_query: str) -> dict[str, object]:
        query = parse_flat_query(
            raw_query,
            singleton=frozenset({
                "revision_id", "revision_ordinal", "tree_digest", "record_id",
                "limit", "cursor",
            }),
            repeated=frozenset({"action", "status"}),
            required=frozenset({"revision_id", "revision_ordinal", "tree_digest", "limit"}),
        )
        return {
            "revision_id": str(query["revision_id"]),
            "ordinal": require_int(query["revision_ordinal"], minimum=1, maximum=2_147_483_647),
            "tree_digest": str(query["tree_digest"]),
            "actions": tuple(query.get("action", ())),
            "statuses": tuple(query.get("status", ())),
            "record_id": str(query["record_id"]) if "record_id" in query else None,
            "limit": require_int(query["limit"], minimum=1, maximum=100),
            "cursor": str(query["cursor"]) if "cursor" in query else None,
        }

    @staticmethod
    def _atlas_proposal_collection_query(raw_query: str) -> dict[str, object]:
        query = parse_flat_query(
            raw_query,
            singleton=frozenset({
                "revision_id", "revision_ordinal", "tree_digest", "record_id",
                "limit", "cursor",
            }),
            repeated=frozenset({"status"}),
            required=frozenset({"revision_id", "revision_ordinal", "tree_digest", "limit"}),
        )
        return {
            "revision_id": str(query["revision_id"]),
            "ordinal": require_int(query["revision_ordinal"], minimum=1, maximum=2_147_483_647),
            "tree_digest": str(query["tree_digest"]),
            "statuses": tuple(query.get("status", ())),
            "record_id": str(query["record_id"]) if "record_id" in query else None,
            "limit": require_int(query["limit"], minimum=1, maximum=100),
            "cursor": str(query["cursor"]) if "cursor" in query else None,
        }

    def _reject_unsupported(self) -> None:
        if self._prepare_request():
            self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)

    do_PUT = _reject_unsupported
    do_PATCH = _reject_unsupported
    do_DELETE = _reject_unsupported
    do_OPTIONS = _reject_unsupported

    def log_message(self, format: str, *args) -> None:
        # Request targets can contain campaign content; emit no access log.
        return None


def _installation_csrf_secret(root: pathlib.Path) -> str:
    path = root / "csrf-secret"
    try:
        value = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        value = secrets.token_hex(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            value = path.read_text(encoding="ascii").strip()
        else:
            with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as stream:
                stream.write(value + "\n")
    if re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise RuntimeError("csrf_secret_invalid")
    return value


def main() -> None:
    snapshot_root = pathlib.Path(os.environ["DRYDOCK_SNAPSHOTS"])
    root = snapshot_root / "runtime"
    provider = SyntheticProvider() if os.environ.get("DRYDOCK_SYNTHETIC_AI") == "1" else None
    receipts = None
    proposal_repository = None
    workflow_repository = None
    ai_repository = None
    atlas_repository = None
    if os.environ.get("DATABASE_URL"):
        try:
            import psycopg
            database_url = os.environ["DATABASE_URL"]
            receipts = PostgresHTTPRepository(lambda: psycopg.connect(database_url))
            proposal_repository = PostgresProposalRepository(lambda: psycopg.connect(database_url))
            workflow_repository = PostgresWorkflowRepository(lambda: psycopg.connect(database_url))
            ai_repository = PostgresAIRepository(lambda: psycopg.connect(database_url))
            atlas_repository = PostgresAtlasProjectionRepository(lambda: psycopg.connect(database_url))
        except ImportError:
            raise RuntimeError("postgres driver unavailable")
    Handler.application = SliceApplication(
        root, snapshot_root=snapshot_root, provider=provider, receipts=receipts,
        proposal_repository=proposal_repository,
        workflow_repository=workflow_repository,
        ai_repository=ai_repository,
        atlas_repository=atlas_repository,
    )
    Handler.csrf_secret = _installation_csrf_secret(root)
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()


if __name__ == "__main__":
    main()
