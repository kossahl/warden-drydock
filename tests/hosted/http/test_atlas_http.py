from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from warden_drydock.hosted.http.application import HTTPFailure, SliceApplication, SyntheticProvider
from warden_drydock.hosted.ai.models import LiveSession
from warden_drydock.hosted.http.contracts import canonical_digest, request_digest_input
from warden_drydock.hosted.http.query import parse_flat_query, require_int, serialize_flat_query
from warden_drydock.hosted.http.repository import InMemoryHTTPRepository, ReceiptConflict
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

    def test_removed_and_forbidden_generation_routes_are_not_registered(self) -> None:
        patterns = tuple(item.pattern for item in _ROUTES.values())
        self.assertNotIn("ask", _ROUTES)
        self.assertFalse(any("/asks" in item for item in patterns))
        self.assertFalse(any("atlas/generations" in item for item in patterns))


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
