from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

from warden_drydock.hosted.http.application import HTTPFailure, SliceApplication, SyntheticProvider
from warden_drydock.hosted.ai.models import LiveSession
from warden_drydock.hosted.http.contracts import canonical_digest, request_digest_input
from warden_drydock.hosted.http.query import parse_flat_query, require_int, serialize_flat_query
from warden_drydock.hosted.operations.server import Handler
from warden_drydock.hosted.http.repository import InMemoryHTTPRepository, ReceiptConflict
from warden_drydock.hosted.projections import encode_cursor
from warden_drydock.hosted.operations.server import _ROUTES


class FlatAtlasQueryTests(unittest.TestCase):
    def test_v1_receipt_cannot_replay_or_be_written_by_v2_repository(self) -> None:
        repository = InMemoryHTTPRepository()
        repository._receipts[("campaign_create", "idem_old")] = (
            "a" * 64, 201, {"contract_name": "campaign_revision_view", "contract_version": 1},
        )
        with self.assertRaisesRegex(ReceiptConflict, "stale_contract_receipt"):
            repository.replay("campaign_create", "idem_old", "a" * 64)
        with self.assertRaisesRegex(ReceiptConflict, "stale_contract_receipt"):
            repository.store("campaign_create", "idem_new", "b" * 64, 201, {"contract_version": 1})

    def test_repeated_filters_are_decoded_deduplicated_and_sorted(self) -> None:
        parsed = parse_flat_query(
            "revision_id=revision_one&revision_ordinal=1&tree_digest=" + "a" * 64
            + "&limit=50&type=npc&type=ship&type=npc&q=Station+Keeper",
            singleton=frozenset({"revision_id", "revision_ordinal", "tree_digest", "limit", "q"}),
            repeated=frozenset({"type"}),
            required=frozenset({"revision_id", "revision_ordinal", "tree_digest", "limit"}),
        )
        self.assertEqual(("npc", "ship"), parsed["type"])
        self.assertEqual("Station Keeper", parsed["q"])
        self.assertEqual(50, require_int(parsed["limit"], minimum=1, maximum=100))

    def test_parser_serializer_parity_for_every_atlas_query_shape(self) -> None:
        binding = {"revision_id": "revision_one", "revision_ordinal": 1, "tree_digest": "a" * 64}
        routes = (
            (binding, frozenset(), frozenset(binding)),
            ({**binding, "q": "Station Keeper", "type": ("ship", "npc"), "authority": ("canon",), "status": ("canon",), "limit": 50, "cursor": None},
             frozenset({"type", "authority", "status"}), frozenset({*binding, "limit"})),
            ({**binding, "depth": 1, "limit": 50, "cursor": None}, frozenset(), frozenset({*binding, "depth", "limit"})),
            ({**binding, "subject_record_id": "campaign-main", "limit": 5, "cursor": None, "direction": "backward"},
             frozenset(), frozenset({*binding, "limit"})),
        )
        for values, repeated, required in routes:
            with self.subTest(values=values):
                encoded = serialize_flat_query(values, repeated=repeated)
                parsed = parse_flat_query(
                    encoded,
                    singleton=frozenset(values) - repeated,
                    repeated=repeated,
                    required=required,
                )
                for name, value in values.items():
                    if value is None:
                        self.assertNotIn(name, parsed)
                    elif name in repeated:
                        self.assertEqual(tuple(sorted(set(value))), parsed[name])
                    else:
                        self.assertEqual(str(value), parsed[name])

    def test_ambiguous_or_malformed_queries_fail_closed(self) -> None:
        invalid = (
            "revision_id=one&revision_id=two",
            "revision_id=one&unknown=value",
            "revision_id=%GG",
            "revision_id=",
            "revisionId=one",
            "type=npc,ship",
            "flag",
        )
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, "invalid_query_binding"):
                parse_flat_query(
                    raw,
                    singleton=frozenset({"revision_id"}),
                    repeated=frozenset({"type"}),
                    required=frozenset({"revision_id"}),
                )

    def test_removed_asks_and_mutating_atlas_generation_routes_are_not_registered(self) -> None:
        patterns = tuple(item.pattern for item in _ROUTES.values())
        self.assertNotIn("ask", _ROUTES)
        self.assertFalse(any("/asks" in item for item in patterns))
        self.assertIn("atlas_generations", _ROUTES)
        self.assertFalse(any("/atlas/generations/" in item for item in patterns))


class AtlasApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.provider = SyntheticProvider()
        self.app = SliceApplication(Path(self.temporary.name), provider=self.provider)

    @staticmethod
    def operation(operation: str, request_id: str, key: str) -> dict:
        return {
            "contract_name": "operation_request", "contract_version": 2,
            "request_id": request_id, "operation": operation,
            "idempotency_key": key, "payload_digest": "0" * 64,
            "expected_revision": None, "expected_workflow_version": None,
        }

    @staticmethod
    def bind(payload: dict) -> dict:
        operation = payload.get("operation_request", payload)
        operation["payload_digest"] = canonical_digest(request_digest_input(payload))
        return payload

    def create(self) -> dict:
        return self.app.create_campaign(self.bind({
            "contract_name": "campaign_create_request", "contract_version": 2,
            "operation_request": self.operation("campaign_create", "request_create", "idem_create"),
            "input": {"campaign_id": "campaign_atlas", "campaign_name": "Atlas Campaign", "adapter_id": "mothership"},
        }))[1]

    def consent(self) -> None:
        identity = self.app.provider_readiness()[1]["consent_identity_digest"]
        self.app.provider_consent(self.bind({
            "contract_name": "provider_consent_request", "contract_version": 2,
            "operation_request": self.operation("provider_consent", "request_consent", "idem_consent"),
            "input": {"explicit": True, "consent_identity_digest": identity},
        }))

    def test_campaign_recovery_atlas_reads_and_summary_are_revision_bound(self) -> None:
        created = self.create()
        viewed = created["viewed_revision"]
        campaigns = self.app.campaign_collection()[1]
        self.assertEqual(("campaign_atlas", "ready"), (
            campaigns["campaigns"][0]["campaign_id"], campaigns["campaigns"][0]["recovery_state"],
        ))
        overview = self.app.atlas_overview(
            "campaign_atlas", viewed["revision_id"], viewed["ordinal"], viewed["tree_digest"]
        )[1]
        self.assertEqual(viewed["revision_id"], overview["binding"]["viewed_revision"]["revision_id"])
        detail = self.app.atlas_record_detail(
            "campaign_atlas", "campaign-main", viewed["revision_id"], viewed["ordinal"], viewed["tree_digest"]
        )[1]
        self.assertEqual("campaign-main", detail["record"]["record_id"])
        summary = self.app.atlas_workflow_summary(
            "campaign_atlas", viewed["revision_id"], viewed["ordinal"], viewed["tree_digest"]
        )[1]
        self.assertEqual(0, summary["draft_generation_count"])
        self.assertNotIn("items", summary)
        with self.assertRaises(HTTPFailure) as caught:
            self.app.atlas_overview(
                "campaign_atlas", viewed["revision_id"], viewed["ordinal"] + 1, viewed["tree_digest"]
            )
        self.assertEqual((422, "invalid_revision_binding"), (
            caught.exception.status, caught.exception.payload["error"]["code"],
        ))

    def test_unsafe_path_bindings_are_rejected_before_lookup(self) -> None:
        created = self.create()
        viewed = created["viewed_revision"]
        for campaign_id, revision_id in (
            ("../private", viewed["revision_id"]),
            ("campaign_atlas", "../private"),
        ):
            with self.subTest(campaign_id=campaign_id, revision_id=revision_id), self.assertRaises(HTTPFailure) as caught:
                self.app.atlas_overview(
                    campaign_id, revision_id, viewed["ordinal"], viewed["tree_digest"]
                )
            self.assertEqual((422, "unsafe_binding"), (
                caught.exception.status, caught.exception.payload["error"]["category"],
            ))

    def test_static_query_and_cursor_failures_do_not_disclose_campaign_existence(self) -> None:
        created = self.create()
        viewed = created["viewed_revision"]
        invalid_cursor = encode_cursor({
            "kind": "record_library", "campaign_id": "campaign_other",
            "revision_id": viewed["revision_id"], "tree_digest": viewed["tree_digest"],
            "normalized_query": "", "record_types": [], "authorities": [],
            "statuses": [], "limit": 50, "sort": "record_id",
            "direction": "forward", "boundary_record_id": "campaign-main",
        })
        for campaign_id in ("campaign_atlas", "campaign_missing"):
            with self.subTest(kind="enum", campaign_id=campaign_id), self.assertRaises(HTTPFailure) as caught:
                self.app.atlas_record_library(
                    campaign_id, viewed["revision_id"], viewed["ordinal"],
                    viewed["tree_digest"], query="", record_types=(),
                    authorities=("private",), statuses=(), limit=50, cursor=None,
                )
            self.assertEqual((422, "invalid_query_binding"), (
                caught.exception.status, caught.exception.payload["error"]["code"],
            ))
            with self.subTest(kind="cursor", campaign_id=campaign_id), self.assertRaises(HTTPFailure) as caught:
                self.app.atlas_record_library(
                    campaign_id, viewed["revision_id"], viewed["ordinal"],
                    viewed["tree_digest"], query="", record_types=(),
                    authorities=(), statuses=(), limit=50, cursor=invalid_cursor,
                )
            self.assertEqual((422, "invalid_cursor_binding"), (
                caught.exception.status, caught.exception.payload["error"]["code"],
            ))
            with self.subTest(kind="neighborhood_cursor", campaign_id=campaign_id), self.assertRaises(HTTPFailure) as caught:
                self.app.atlas_neighborhood(
                    campaign_id, "campaign-main", viewed["revision_id"],
                    viewed["ordinal"], viewed["tree_digest"], depth=1,
                    limit=50, cursor="malformed",
                )
            self.assertEqual((422, "invalid_cursor_binding"), (
                caught.exception.status, caught.exception.payload["error"]["code"],
            ))
            with self.subTest(kind="history_cursor", campaign_id=campaign_id), self.assertRaises(HTTPFailure) as caught:
                self.app.atlas_history(
                    campaign_id, viewed["revision_id"], viewed["ordinal"],
                    viewed["tree_digest"], subject_record_id=None, limit=50,
                    cursor="malformed", direction="forward",
                )
            self.assertEqual((422, "invalid_cursor_binding"), (
                caught.exception.status, caught.exception.payload["error"]["code"],
            ))

    def test_removed_atlas_generation_contract_is_not_exported(self) -> None:
        import warden_drydock.hosted.projections as projections

        self.assertFalse(hasattr(projections, "contextual_generation_contract"))

    def test_real_handler_maps_every_atlas_get_and_rejects_ambiguous_queries(self) -> None:
        created = self.create()
        viewed = created["viewed_revision"]
        Handler.application = self.app
        static = Path(self.temporary.name) / "static"
        static.mkdir()
        (static / "index.html").write_text("ok", encoding="utf-8")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        prior_hosts = os.environ.get("DRYDOCK_ALLOWED_HOSTS")
        prior_static = os.environ.get("DRYDOCK_STATIC")
        os.environ["DRYDOCK_ALLOWED_HOSTS"] = f"127.0.0.1:{server.server_port}"
        os.environ["DRYDOCK_STATIC"] = str(static)
        thread.start()

        def cleanup() -> None:
            server.shutdown()
            thread.join(5)
            server.server_close()
            Handler.application = None
            if prior_hosts is None:
                os.environ.pop("DRYDOCK_ALLOWED_HOSTS", None)
            else:
                os.environ["DRYDOCK_ALLOWED_HOSTS"] = prior_hosts
            if prior_static is None:
                os.environ.pop("DRYDOCK_STATIC", None)
            else:
                os.environ["DRYDOCK_STATIC"] = prior_static

        self.addCleanup(cleanup)
        base = f"http://127.0.0.1:{server.server_port}/api/v1"
        binding = urllib.parse.urlencode({
            "revision_id": viewed["revision_id"],
            "revision_ordinal": viewed["ordinal"],
            "tree_digest": viewed["tree_digest"],
        })
        routes = (
            ("/campaigns", "atlas_campaign_collection"),
            (f"/campaigns/campaign_atlas/atlas/overview?{binding}", "atlas_overview"),
            (f"/campaigns/campaign_atlas/atlas/records?{binding}&limit=50", "atlas_record_library_result"),
            (f"/campaigns/campaign_atlas/atlas/records/campaign-main?{binding}", "atlas_record_detail"),
            (f"/campaigns/campaign_atlas/atlas/records/campaign-main/neighborhood?{binding}&depth=1&limit=50", "atlas_depth_1_neighborhood"),
            (f"/campaigns/campaign_atlas/atlas/history?{binding}&limit=50", "atlas_approved_history_collection"),
            (f"/campaigns/campaign_atlas/atlas/workflow-summary?{binding}", "atlas_workflow_summary"),
            (f"/campaigns/campaign_atlas/atlas/generations?{binding}&limit=50", "atlas_generation_collection"),
            (f"/campaigns/campaign_atlas/atlas/proposals?{binding}&limit=50", "atlas_proposal_collection"),
        )
        for route, contract_name in routes:
            with self.subTest(route=route), urllib.request.urlopen(base + route) as response:
                payload = json.load(response)
                self.assertEqual((200, contract_name), (response.status, payload["contract_name"]))
                self.assertNotIn("items", payload if contract_name == "atlas_workflow_summary" else {})

        invalid = (
            f"/campaigns/campaign_atlas/atlas/overview?{binding}&unknown=x",
            f"/campaigns/campaign_atlas/atlas/overview?{binding}&revision_id=revision_other",
            "/campaigns/campaign_atlas/atlas/overview?revision_id=%GG",
            f"/campaigns/campaign_atlas/atlas/records?{binding}",
            f"/campaigns/campaign_atlas/atlas/proposals?{binding}&action=generate&limit=50",
        )
        for route in invalid:
            with self.subTest(invalid=route), self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(base + route)
            self.assertEqual(422, caught.exception.code)
            self.assertEqual("unsafe_binding", json.load(caught.exception)["error"]["category"])
            caught.exception.close()

        cursor = encode_cursor({
            "kind": "record_library", "campaign_id": "campaign_other",
            "revision_id": viewed["revision_id"], "tree_digest": viewed["tree_digest"],
            "normalized_query": "", "record_types": [], "authorities": [],
            "statuses": [], "limit": 50, "sort": "record_id", "direction": "forward",
            "boundary_record_id": "campaign-main",
        })
        route = f"/campaigns/campaign_atlas/atlas/records?{binding}&limit=50&cursor={urllib.parse.quote(cursor)}"
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(base + route)
        payload = json.load(caught.exception)
        self.assertEqual((422, "invalid_cursor_binding"), (
            caught.exception.code, payload["error"]["code"],
        ))
        caught.exception.close()

    def test_record_context_is_verified_before_retrieval_or_dispatch_and_replay_is_exact(self) -> None:
        created = self.create()
        viewed = created["viewed_revision"]
        detail = self.app.atlas_record_detail(
            "campaign_atlas", "campaign-main", viewed["revision_id"], viewed["ordinal"], viewed["tree_digest"]
        )[1]
        self.consent()
        request = {
            "contract_name": "generation_start_request", "contract_version": 2,
            "generation_id": "generation_record", "campaign_id": "campaign_atlas",
            "source_revision": viewed["revision_id"], "action": "check",
            "prompt": "Check the campaign record.",
            "context": {"scope": "record", "record_id": "campaign-main", "content_digest": "0" * 64},
        }
        with self.assertRaises(HTTPFailure) as caught:
            self.app.start_generation("campaign_atlas", viewed["revision_id"], request)
        self.assertEqual((422, "invalid_generation_binding", 0), (
            caught.exception.status, caught.exception.payload["error"]["code"], self.provider.calls,
        ))
        self.assertEqual({}, self.app.ai_repository.generations)

        request["context"]["content_digest"] = detail["record"]["content_digest"]
        status, response, dispatch = self.app.start_generation(
            "campaign_atlas", viewed["revision_id"], request
        )
        self.assertEqual((202, True, request["context"], 0), (
            status, dispatch, response["context"], self.provider.calls,
        ))
        changed = deepcopy(request)
        changed["action"] = "generate"
        with self.assertRaises(HTTPFailure) as replay:
            self.app.start_generation("campaign_atlas", viewed["revision_id"], changed)
        self.assertEqual((409, 0), (replay.exception.status, self.provider.calls))

    def test_session_revision_mismatch_rejects_before_retrieval_or_dispatch(self) -> None:
        created = self.create()
        self.consent()
        self.app.ai_repository.create_session(LiveSession(
            "session_atlas", "campaign_atlas", created["head_revision"], created["head_revision"]
        ))
        request = {
            "contract_name": "generation_start_request", "contract_version": 2,
            "generation_id": "generation_session", "campaign_id": "campaign_atlas",
            "source_revision": "revision_other", "action": "ask", "prompt": "Check.",
            "context": {"scope": "campaign"}, "session_id": "session_atlas",
        }
        with self.assertRaises(HTTPFailure) as caught:
            self.app.start_generation("campaign_atlas", "revision_other", request)
        self.assertEqual((422, 0, {}), (
            caught.exception.status, self.provider.calls, self.app.ai_repository.generations,
        ))


if __name__ == "__main__":
    unittest.main()
