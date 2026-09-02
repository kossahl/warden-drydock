from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from warden_drydock.hosted.ai.repository import InMemoryAIRepository
from warden_drydock.hosted.http.application import HTTPFailure, SliceApplication, SyntheticProvider
from warden_drydock.hosted.http.contracts import canonical_digest, request_digest_input
from warden_drydock.hosted.operations.server import Handler


class LiveHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.app = SliceApplication(Path(self.temporary.name), provider=SyntheticProvider())
        self.campaign_id, self.head_revision = self._create_campaign()

    @staticmethod
    def operation(operation: str, request_id: str, key: str, *, expected_workflow_version: int | None = None) -> dict:
        return {"contract_name": "operation_request", "contract_version": 2,
                "request_id": request_id, "operation": operation,
                "idempotency_key": key, "payload_digest": "0" * 64,
                "expected_revision": None, "expected_workflow_version": expected_workflow_version}

    @staticmethod
    def bind(payload: dict) -> dict:
        payload["operation_request"]["payload_digest"] = canonical_digest(request_digest_input(payload))
        return payload

    def _create_campaign(self, campaign_id="campaign_alpha") -> tuple[str, str]:
        payload = {"contract_name": "campaign_create_request", "contract_version": 2,
                   "operation_request": self.operation("campaign_create", "request_campaign", "idem_campaign"),
                   "input": {"campaign_id": campaign_id, "campaign_name": "Live Campaign", "adapter_id": "mothership"}}
        status, response = self.app.create_campaign(self.bind(payload))
        self.assertEqual(201, status)
        return campaign_id, response["head_revision"]

    def start(self, *, session_id="session_alpha", controller_id="controller_alpha", key="idem_start") -> dict:
        payload = {"contract_name": "live_start_request", "contract_version": 2,
                   "operation_request": self.operation("live_start", "request_start", key),
                   "campaign_id": self.campaign_id, "session_id": session_id,
                   "head_revision": self.head_revision, "controller_id": controller_id}
        status, response = self.app.live_start(self.campaign_id, self.bind(payload))
        self.assertEqual(201, status)
        self.assertEqual("live_session_view", response["contract_name"])
        return response

    def _capture_body(self, *, expected_workflow_version=1, controller_id="controller_alpha",
                      event_id="event_fact", device_id="device_one", operation_id="operation_fact",
                      text="Door opened into the station.", capture_type="confirmed_fact",
                      record_id="record-one", session_id="session_alpha", key="idem_capture",
                      device_order=1) -> dict:
        payload = {"contract_name": "live_capture_request", "contract_version": 2,
                   "operation_request": self.operation("live_capture", "request_capture", key,
                                                       expected_workflow_version=expected_workflow_version),
                   "campaign_id": self.campaign_id, "session_id": session_id,
                   "controller_id": controller_id, "controller_epoch": 1,
                   "event_id": event_id, "device_id": device_id, "operation_id": operation_id,
                   "device_order": device_order, "capture_type": capture_type, "text": text, "record_id": record_id}
        return self.bind(payload)

    def capture(self, **kwargs):
        self.app.live_capture(self.campaign_id, self._capture_body(**kwargs))

    def _capture_dispatch(self, **kwargs):
        return self.app.live_capture(self.campaign_id, self._capture_body(**kwargs))

    def end(self, *, expected_workflow_version, required_operation_ids, controller_id="controller_alpha",
            operation_id="operation_end", key="idem_end", device_id="device_one"):
        payload = {"contract_name": "live_end_request", "contract_version": 2,
                   "operation_request": self.operation("live_end", "request_end", key,
                                                       expected_workflow_version=expected_workflow_version),
                   "campaign_id": self.campaign_id, "session_id": "session_alpha",
                   "controller_id": controller_id, "controller_epoch": 1,
                   "device_id": device_id, "operation_id": operation_id,
                   "required_operation_ids": (required_operation_ids if required_operation_ids and isinstance(required_operation_ids[0], dict) else [{"device_id": d, "operation_id": o} for d, o in required_operation_ids])}
        return self.app.live_end(self.campaign_id, self.bind(payload))

    def test_start_read_takeover_capture_end_flow(self) -> None:
        view = self.start()
        self.assertEqual("active", view["mode"])
        self.assertEqual(self.head_revision, view["base_revision"])
        read = self.app.live_read(self.campaign_id)[1]
        self.assertEqual("session_alpha", read["session_id"])

        captured, result = self._capture_dispatch()
        self.assertEqual(200, captured)
        self.assertEqual("accepted", result["outcome"])
        self.assertEqual("record-one", result["session"]["events"][0]["record_id"])

        ended, response = self.end(expected_workflow_version=2, required_operation_ids=[("device_one", "operation_fact")])
        self.assertEqual(200, ended)
        self.assertEqual("ended_review_pending", response["mode"])
        self.assertTrue(response["end_barrier"]["ready_for_proposal"])
        self.assertEqual([{"device_id":"device_one","operation_id":"operation_fact"}], response["end_barrier"]["required_operation_ids"])
        self.assertEqual([{"device_id":"device_one","operation_id":"operation_fact"}], response["end_barrier"]["acknowledged_operation_ids"])

    def test_capture_rejects_stale_or_duplicate_device_order_and_allows_gap(self):
        self.start()
        self.capture(device_order=2)
        self.capture(event_id="event_four", operation_id="operation_four", device_order=4, expected_workflow_version=2)
        with self.assertRaises(HTTPFailure) as ctx:
            self.capture(event_id="event_dup", operation_id="operation_dup", device_order=4, expected_workflow_version=3)
        self.assertEqual(422, ctx.exception.status)
        self.assertEqual("unsafe_binding", ctx.exception.payload["error"]["category"])
        with self.assertRaises(HTTPFailure) as ctx:
            self.capture(event_id="event_old", operation_id="operation_old", device_order=1, expected_workflow_version=3)
        self.assertEqual(422, ctx.exception.status)
        self.assertEqual("unsafe_binding", ctx.exception.payload["error"]["category"])

    def test_end_barrier_preserves_exact_multi_device_identities(self):
        self.start()
        self.capture(device_order=1)
        self.capture(device_id="device_two", event_id="event_two", operation_id="operation_fact", device_order=1, expected_workflow_version=2)
        _, response = self.end(expected_workflow_version=3, required_operation_ids=[("device_one", "operation_fact"), ("device_two", "operation_fact")])
        self.assertTrue(response["end_barrier"]["ready_for_proposal"])
        self.assertEqual(2, len(response["end_barrier"]["acknowledged_operation_ids"]))

    def test_end_barrier_excludes_only_exact_end_identity_and_rejects_bad_identity_lists(self):
        self.start()
        with self.assertRaises(HTTPFailure) as malformed:
            self.end(expected_workflow_version=1, required_operation_ids=[{"operation_id":"operation_x"}])
        self.assertEqual((422, "unsafe_binding"), (malformed.exception.status, malformed.exception.payload["error"]["category"]))
        with self.assertRaises(HTTPFailure) as duplicate:
            self.end(expected_workflow_version=1, required_operation_ids=[("device_one", "operation_x"), ("device_one", "operation_x")])
        self.assertEqual((422, "unsafe_binding"), (duplicate.exception.status, duplicate.exception.payload["error"]["category"]))
        self.capture(device_id="device_two", event_id="event_same", operation_id="operation_end", device_order=1, expected_workflow_version=1)
        _, response = self.end(expected_workflow_version=2, required_operation_ids=[("device_two", "operation_end")], operation_id="operation_end")
        self.assertIn({"device_id":"device_two", "operation_id":"operation_end"}, response["end_barrier"]["acknowledged_operation_ids"])

    def test_capture_exact_replay_and_digest_conflict(self) -> None:
        self.start()
        self.capture()
        status, result = self._capture_dispatch()
        self.assertEqual(200, status)
        self.assertEqual("exact_replay", result["outcome"])
        with self.assertRaises(HTTPFailure) as conflict:
            self._capture_dispatch(text="Changed", key="idem_capture_other")
        self.assertEqual(409, conflict.exception.status)
        self.assertEqual("idempotency_digest_conflict", conflict.exception.payload["error"]["code"])

    def test_stale_controller_returns_409(self) -> None:
        self.start()
        with self.assertRaises(HTTPFailure) as stale:
            self._capture_dispatch(controller_id="intruder")
        self.assertEqual(409, stale.exception.status)
        self.assertEqual("stale_controller", stale.exception.payload["error"]["category"])

    def test_stale_workflow_returns_409(self) -> None:
        self.start()
        with self.assertRaises(HTTPFailure) as stale:
            self._capture_dispatch(expected_workflow_version=99)
        self.assertEqual(409, stale.exception.status)
        self.assertEqual("stale_workflow", stale.exception.payload["error"]["category"])

    def test_takeover_invalidates_old_controller_over_http(self) -> None:
        self.start()
        payload = {"contract_name": "live_takeover_request", "contract_version": 2,
                   "operation_request": self.operation("live_takeover", "request_takeover", "idem_takeover",
                                                       expected_workflow_version=1),
                   "campaign_id": self.campaign_id, "session_id": "session_alpha",
                   "controller_id": "controller_beta", "controller_epoch": 1}
        status, view = self.app.live_takeover(self.campaign_id, self.bind(payload))
        self.assertEqual(200, status)
        self.assertEqual("controller_beta", view["controller"]["controller_id"])
        with self.assertRaises(HTTPFailure) as stale:
            self._capture_dispatch(controller_id="controller_alpha")
        self.assertEqual(409, stale.exception.status)

    def test_end_barrier_fails_closed_when_hiding_capture(self) -> None:
        self.start()
        self.capture(operation_id="operation_fact", text="Fact one")
        self.capture(event_id="event_two", operation_id="operation_two", device_order=2, text="Fact two",
                     expected_workflow_version=2)
        with self.assertRaises(HTTPFailure) as rejected:
            self.end(expected_workflow_version=3, required_operation_ids=[("device_one", "operation_fact")])
        self.assertEqual(409, rejected.exception.status)
        self.assertEqual("live_barrier_conflict", rejected.exception.payload["error"]["category"])

    def test_restart_recovers_ended_session_with_full_state(self) -> None:
        self.start()
        self.capture(operation_id="operation_fact", text="Fact one", record_id="record-door")
        self.end(expected_workflow_version=2, required_operation_ids=[("device_one", "operation_fact")])
        # P2-6: a genuinely fresh repository seeded from persisted state must restore
        # the ended session with captures, receipts, end barrier, provenance, and the
        # persisted monotonic ordering. The ordering marker (session_seq) lives on the
        # session records themselves, so selection works from persisted data alone.
        fresh = InMemoryAIRepository()
        fresh.sessions = {session_id: deepcopy(item) for session_id, item in self.app.ai_repository.sessions.items()}
        restored = SliceApplication(
            Path(self.temporary.name) / "restarted", provider=SyntheticProvider(),
            ai_repository=fresh,
        )
        status, view = restored.live_read(self.campaign_id)
        self.assertEqual(200, status)
        self.assertEqual("ended_review_pending", view["mode"])
        self.assertEqual(["record-door"], [item["record_id"] for item in view["events"]])
        self.assertEqual([{"device_id":"device_one","operation_id":"operation_fact"}], view["end_barrier"]["required_operation_ids"])
        self.assertEqual([{"device_id":"device_one","operation_id":"operation_fact"}], view["end_barrier"]["acknowledged_operation_ids"])
        self.assertTrue(view["end_barrier"]["ready_for_proposal"])

    def test_start_rejects_nonexistent_revision_and_campaign(self) -> None:
        payload = {"contract_name": "live_start_request", "contract_version": 2,
                   "operation_request": self.operation("live_start", "request_start", "idem_start_bad"),
                   "campaign_id": self.campaign_id, "session_id": "session_bad",
                   "head_revision": "revision_missing", "controller_id": "controller_alpha"}
        with self.assertRaises(HTTPFailure) as missing_revision:
            self.app.live_start(self.campaign_id, self.bind(payload))
        self.assertEqual(404, missing_revision.exception.status)
        self.assertEqual("revision_not_found", missing_revision.exception.payload["error"]["code"])

        payload["operation_request"] = self.operation("live_start", "request_start", "idem_start_bad2")
        payload["head_revision"] = self.head_revision
        payload["campaign_id"] = "campaign_missing"
        with self.assertRaises(HTTPFailure) as missing_campaign:
            self.app.live_start("campaign_missing", self.bind(payload))
        self.assertEqual(404, missing_campaign.exception.status)

    def test_start_rejects_path_campaign_mismatch(self) -> None:
        payload = {"contract_name": "live_start_request", "contract_version": 2,
                   "operation_request": self.operation("live_start", "request_start", "idem_start_mismatch"),
                   "campaign_id": self.campaign_id, "session_id": "session_mismatch",
                   "head_revision": self.head_revision, "controller_id": "controller_alpha"}
        with self.assertRaises(HTTPFailure) as mismatched:
            self.app.live_start("campaign_other", self.bind(payload))
        self.assertEqual(422, mismatched.exception.status)

    def test_http_end_rejects_self_certifying_required_set(self) -> None:
        # P1-A: the end operation cannot self-certify readiness - an end request
        # naming its own operation_id in required_operation_ids is rejected 422.
        self.start()
        with self.assertRaises(HTTPFailure) as rejected:
            self.end(expected_workflow_version=1, required_operation_ids=[("device_one", "operation_end")])
        self.assertEqual(422, rejected.exception.status)

    def test_get_is_strictly_read_only(self) -> None:
        # P2-C: GET live_read never mutates persisted state.
        self.start()
        before = self.app.live_read(self.campaign_id)[1]
        again = self.app.live_read(self.campaign_id)[1]
        self.assertEqual(before, again)
        self.assertEqual("active", before["mode"])
        # reported_head_revision is unchanged (no side effect).
        self.assertEqual(self.head_revision, before["reported_head_revision"])

    def test_live_read_returns_highest_seq_and_404_when_absent(self) -> None:
        # P1, Option C: readback returns the highest persisted session_seq and there
        # is no ambiguity error; an absent session is 404.
        self.start()  # session_alpha
        self.capture()
        self.end(expected_workflow_version=2, required_operation_ids=[("device_one", "operation_fact")])
        second_start = {"contract_name": "live_start_request", "contract_version": 2,
                        "operation_request": self.operation("live_start", "request_start_2", "idem_start_2"),
                        "campaign_id": self.campaign_id, "session_id": "session_two",
                        "head_revision": self.head_revision, "controller_id": "controller_alpha"}
        self.app.live_start(self.campaign_id, self.bind(second_start))
        second_end = {"contract_name": "live_end_request", "contract_version": 2,
                      "operation_request": self.operation("live_end", "request_end_2", "idem_end_2", expected_workflow_version=1),
                      "campaign_id": self.campaign_id, "session_id": "session_two",
                      "controller_id": "controller_alpha", "controller_epoch": 1,
                      "device_id": "device_one", "operation_id": "operation_end_2",
                      "required_operation_ids": []}
        self.app.live_end(self.campaign_id, self.bind(second_end))
        _, view = self.app.live_read(self.campaign_id)
        self.assertEqual("session_two", view["session_id"])
        # No ambiguity error path exists; absent session is 404.
        with self.assertRaises(HTTPFailure) as missing:
            self.app.live_read("campaign_absent")
        self.assertEqual(404, missing.exception.status)
        self.assertEqual("not_found", missing.exception.payload["error"]["category"])

    def test_live_routes_registered_in_server(self) -> None:
        from warden_drydock.hosted.operations import server
        paths = {
            "live_session": f"/api/v1/campaigns/{self.campaign_id}/live/session",
            "live_takeover": f"/api/v1/campaigns/{self.campaign_id}/live/session/takeover",
            "live_capture": f"/api/v1/campaigns/{self.campaign_id}/live/session/captures",
            "live_end": f"/api/v1/campaigns/{self.campaign_id}/live/session/end",
        }
        for name, path in paths.items():
            with self.subTest(route=name):
                self.assertIsNotNone(server._ROUTES[name].fullmatch(path),
                                     f"route {name} not registered")


class LiveHTTPRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.app = SliceApplication(Path(self.temporary.name), provider=SyntheticProvider())

    @staticmethod
    def operation(operation: str, request_id: str, key: str, *, expected_workflow_version: int | None = None) -> dict:
        return {"contract_name": "operation_request", "contract_version": 2,
                "request_id": request_id, "operation": operation,
                "idempotency_key": key, "payload_digest": "0" * 64,
                "expected_revision": None, "expected_workflow_version": expected_workflow_version}

    @staticmethod
    def bind(payload: dict) -> dict:
        payload["operation_request"]["payload_digest"] = canonical_digest(request_digest_input(payload))
        return payload

    def _create_campaign(self) -> str:
        payload = {"contract_name": "campaign_create_request", "contract_version": 2,
                   "operation_request": self.operation("campaign_create", "request_campaign", "idem_campaign"),
                   "input": {"campaign_id": "campaign_alpha", "campaign_name": "Live Campaign", "adapter_id": "mothership"}}
        status, response = self.app.create_campaign(self.bind(payload))
        self.assertEqual(201, status)
        return response["head_revision"]

    def _server(self):
        static = Path(self.temporary.name) / "static"
        static.mkdir()
        (static / "index.html").write_text("ok", encoding="utf-8")
        Handler.application = self.app
        Handler.csrf_secret = "a" * 64
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        original = {name: os.environ.get(name) for name in ("DRYDOCK_ALLOWED_HOSTS", "DRYDOCK_STATIC")}
        os.environ["DRYDOCK_ALLOWED_HOSTS"] = f"127.0.0.1:{server.server_port}"
        os.environ["DRYDOCK_STATIC"] = str(static)
        thread.start()

        def cleanup():
            server.shutdown()
            thread.join(5)
            server.server_close()
            for name, value in original.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            Handler.application = None
            Handler.csrf_secret = None
        self.addCleanup(cleanup)
        return server

    def _csrf(self, base: str):
        with urllib.request.urlopen(base + "/api/v1/provider/readiness") as response:
            csrf = response.headers["X-CSRF-Token"]
            cookie = response.headers["Set-Cookie"].split(";", 1)[0]
        return csrf, cookie

    def _post(self, base: str, csrf: str, cookie: str, path: str, payload: dict):
        body = json.dumps(payload).encode()
        request = urllib.request.Request(
            base + path, data=body,
            headers={"Content-Type": "application/json", "X-CSRF-Token": csrf, "Cookie": cookie},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            return response.status, json.load(response)

    def test_post_requires_csrf_and_capture_round_trips(self) -> None:
        head_revision = self._create_campaign()
        server = self._server()
        base = f"http://127.0.0.1:{server.server_port}"
        csrf, cookie = self._csrf(base)
        start_payload = {"contract_name": "live_start_request", "contract_version": 2,
                         "operation_request": self.operation("live_start", "request_start", "idem_start"),
                         "campaign_id": "campaign_alpha", "session_id": "session_alpha",
                         "head_revision": head_revision, "controller_id": "controller_alpha"}
        body = json.dumps(self.bind(start_payload)).encode()
        request = urllib.request.Request(
            base + "/api/v1/campaigns/campaign_alpha/live/session", data=body,
            headers={"Content-Type": "application/json", "Cookie": cookie}, method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            urllib.request.urlopen(request)
        self.assertEqual(403, rejected.exception.code)
        rejected.exception.close()

        status, view = self._post(base, csrf, cookie, "/api/v1/campaigns/campaign_alpha/live/session", self.bind(start_payload))
        self.assertEqual(201, status)
        self.assertEqual("active", view["mode"])

        capture_payload = {"contract_name": "live_capture_request", "contract_version": 2,
                           "operation_request": self.operation("live_capture", "request_capture", "idem_capture", expected_workflow_version=1),
                           "campaign_id": "campaign_alpha", "session_id": "session_alpha",
                           "controller_id": "controller_alpha", "controller_epoch": 1,
                           "event_id": "event_fact", "device_id": "device_one", "operation_id": "operation_fact",
                           "device_order": 1, "capture_type": "confirmed_fact",
                           "text": "Door opened into the station.", "record_id": "record-one"}
        capture_status, result = self._post(base, csrf, cookie, "/api/v1/campaigns/campaign_alpha/live/session/captures", self.bind(capture_payload))
        self.assertEqual(200, capture_status)
        self.assertEqual("accepted", result["outcome"])

        read = json.load(urllib.request.urlopen(base + "/api/v1/campaigns/campaign_alpha/live/session"))
        self.assertEqual(1, len(read["events"]))

        end_payload = {"contract_name": "live_end_request", "contract_version": 2,
                       "operation_request": self.operation("live_end", "request_end", "idem_end", expected_workflow_version=2),
                       "campaign_id": "campaign_alpha", "session_id": "session_alpha",
                       "controller_id": "controller_alpha", "controller_epoch": 1,
                       "device_id": "device_one", "operation_id": "operation_end",
                       "required_operation_ids": [{"device_id":"device_one", "operation_id":"operation_fact"}]}
        end_status, ended = self._post(base, csrf, cookie, "/api/v1/campaigns/campaign_alpha/live/session/end", self.bind(end_payload))
        self.assertEqual(200, end_status)
        self.assertTrue(ended["end_barrier"]["ready_for_proposal"])

        # P1-3: the ended session remains readable through the GET route.
        read_ended = json.load(urllib.request.urlopen(base + "/api/v1/campaigns/campaign_alpha/live/session"))
        self.assertEqual("ended_review_pending", read_ended["mode"])

    def test_get_rejects_any_query_parameter(self) -> None:
        head_revision = self._create_campaign()
        server = self._server()
        base = f"http://127.0.0.1:{server.server_port}"
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            urllib.request.urlopen(base + f"/api/v1/campaigns/campaign_alpha/live/session?reported_head_revision=%40%40%40")
        self.assertEqual(422, rejected.exception.code)
        rejected.exception.close()


if __name__ == "__main__":
    unittest.main()
