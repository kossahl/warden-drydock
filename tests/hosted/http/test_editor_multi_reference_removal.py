import unittest

from warden_drydock.hosted.http.application import HTTPFailure
from warden_drydock.hosted.http.contracts import canonical_digest, request_digest_input

from tests.hosted.http import test_editor_backend


class MultiReferenceRemovalTests(unittest.TestCase):
    """Removal proposals aggregate engine changes by source record."""

    def setUp(self):
        self.backend = test_editor_backend.EditorBackendTests("runTest")
        self.backend.setUp()
        self.addCleanup(self.backend.doCleanups)
        self.app = self.backend.app

    def _remove_payload(self, revision, impact, resolutions, key="idem_remove_multi"):
        workflow = self.app._editor_version("campaign_alpha")
        operation = {
            "contract_name": "editor_operation_request",
            "contract_version": 1,
            "request_id": f"request_{key}",
            "operation": "editor_record_remove",
            "idempotency_key": key,
            "payload_digest": "0" * 64,
            "expected_revision": revision,
            "expected_editor_workflow_version": workflow,
            "subject_id": impact["binding"]["record_id"],
        }
        payload = {
            "contract_name": "editor_record_remove_request",
            "contract_version": 1,
            "operation_request": operation,
            "binding": impact["binding"],
            "impact_digest": impact["impact_digest"],
            "impact_binding": {
                "binding": impact["binding"],
                "impact_digest": impact["impact_digest"],
            },
            "resolutions": resolutions,
        }
        operation["payload_digest"] = canonical_digest(request_digest_input(payload))
        return payload

    def test_same_source_references_stage_once_and_publish_both_resolutions(self):
        self.backend._create_record("record-target")
        revision = self.backend._create_record(
            "record-source",
            connections=[
                {
                    "connection_id": "connection_one",
                    "target_record_id": "record-target",
                    "relationship": "related-to",
                    "state": "current",
                    "context": "First context.",
                },
                {
                    "connection_id": "connection_two",
                    "target_record_id": "record-target",
                    "relationship": "related-to",
                    "state": "current",
                    "context": "Second context.",
                },
            ],
        )
        _, impact = self.app.editor_removal_impact("campaign_alpha", revision, "record-target")
        resolutions = [
            {"reference_id": impact["incoming_references"][0]["reference_id"], "action": "remove_reference", "replacement_target_record_id": None},
            {"reference_id": impact["incoming_references"][1]["reference_id"], "action": "remove_reference", "replacement_target_record_id": None},
        ]
        payload = self._remove_payload(revision, impact, resolutions)
        head_before = self.app.workflow.head("campaign_alpha")
        _, proposal = self.app.editor_record_remove("campaign_alpha", revision, "record-target", payload)

        self.assertEqual(head_before, self.app.workflow.head("campaign_alpha"))
        item = self.app.proposal_repository.get(proposal["proposal_id"], proposal["proposal_version"])
        self.assertEqual(2, len(item.changes))
        self.assertEqual(2, len(proposal["record_bindings"]))
        self.assertEqual(
            {"record-target", "record-source"},
            {binding["record_id"] for binding in proposal["record_bindings"]},
        )
        self.assertEqual(2, proposal["diff"]["affected_record_count"])
        resolution_cards = [card for card in proposal["diff"]["cards"] if card["kind"] == "reference_resolution"]
        self.assertEqual(2, len(resolution_cards))
        self.assertEqual(2, len({card["change_id"] for card in resolution_cards}))
        self.assertEqual(
            {item["reference_id"] for item in impact["incoming_references"]},
            {card["before"]["reference_id"] for card in resolution_cards},
        )

        self.backend._approve_editor(proposal)
        new_revision = self.app.workflow.head("campaign_alpha")
        with self.assertRaises(HTTPFailure):
            self.app.editor_record_read("campaign_alpha", new_revision, "record-target")
        source = self.app.editor_record_read("campaign_alpha", new_revision, "record-source")[1]["record"]
        self.assertEqual([], source["connections"])

        old_source = self.app.editor_record_read("campaign_alpha", revision, "record-source")[1]
        self.assertTrue(old_source["historical"])
        self.assertEqual(2, len(old_source["record"]["connections"]))
        _, readback = self.app.editor_proposal_read(proposal["proposal_id"], proposal["proposal_version"])
        self.assertEqual("published", readback["publication"]["status"])
        self.assertEqual(new_revision, readback["publication"]["published_revision"]["revision_id"])

    def test_same_source_mixed_redirect_and_remove_preserves_remaining_connection(self):
        self.backend._create_record("record-target")
        self.backend._create_record("record-replacement")
        revision = self.backend._create_record(
            "record-source",
            connections=[
                {
                    "connection_id": "connection_redirect",
                    "target_record_id": "record-target",
                    "relationship": "related-to",
                    "state": "current",
                    "context": "Redirect this context.",
                },
                {
                    "connection_id": "connection_remove",
                    "target_record_id": "record-target",
                    "relationship": "related-to",
                    "state": "current",
                    "context": "Remove this context.",
                },
            ],
        )
        _, impact = self.app.editor_removal_impact("campaign_alpha", revision, "record-target")
        resolutions = [
            {
                "reference_id": impact["incoming_references"][0]["reference_id"],
                "action": "redirect",
                "replacement_target_record_id": "record-replacement",
            },
            {"reference_id": impact["incoming_references"][1]["reference_id"], "action": "remove_reference", "replacement_target_record_id": None},
        ]
        payload = self._remove_payload(revision, impact, resolutions, key="idem_remove_mixed")
        _, proposal = self.app.editor_record_remove("campaign_alpha", revision, "record-target", payload)
        self.assertEqual(2, len(self.app.proposal_repository.get(proposal["proposal_id"], 1).changes))
        self.backend._approve_editor(proposal)

        source = self.app.editor_record_read("campaign_alpha", self.app.workflow.head("campaign_alpha"), "record-source")[1]["record"]
        self.assertEqual(1, len(source["connections"]))
        self.assertEqual("record-replacement", source["connections"][0]["target_record_id"])
        self.assertEqual("Redirect this context.", source["connections"][0]["context"])

    def test_multiple_sources_stage_one_change_and_binding_per_source(self):
        self.backend._create_record("record-target")
        self.backend._create_record("record-replacement")
        self.backend._create_record(
            "record-source-one",
            connections=[{
                "connection_id": "connection_one",
                "target_record_id": "record-target",
                "relationship": "related-to",
                "state": "current",
                "context": "One context.",
            }],
        )
        revision = self.backend._create_record(
            "record-source-two",
            connections=[{
                "connection_id": "connection_two",
                "target_record_id": "record-target",
                "relationship": "related-to",
                "state": "current",
                "context": "Two context.",
            }],
        )
        _, impact = self.app.editor_removal_impact("campaign_alpha", revision, "record-target")
        resolutions = [
            {"reference_id": item["reference_id"], "action": "redirect", "replacement_target_record_id": "record-replacement"}
            for item in impact["incoming_references"]
        ]
        payload = self._remove_payload(revision, impact, resolutions, key="idem_remove_sources")
        _, proposal = self.app.editor_record_remove("campaign_alpha", revision, "record-target", payload)
        item = self.app.proposal_repository.get(proposal["proposal_id"], 1)
        self.assertEqual(3, len(item.changes))
        self.assertEqual(3, len(proposal["record_bindings"]))
        self.assertEqual(3, proposal["diff"]["affected_record_count"])

    def test_incomplete_resolution_fails_before_claim_and_preserves_head(self):
        self.backend._create_record("record-target")
        revision = self.backend._create_record(
            "record-source",
            connections=[
                {"connection_id": "connection_one", "target_record_id": "record-target", "relationship": "related-to", "state": "current", "context": "One."},
                {"connection_id": "connection_two", "target_record_id": "record-target", "relationship": "related-to", "state": "current", "context": "Two."},
            ],
        )
        _, impact = self.app.editor_removal_impact("campaign_alpha", revision, "record-target")
        resolution = {"reference_id": impact["incoming_references"][0]["reference_id"], "action": "remove_reference", "replacement_target_record_id": None}
        payload = self._remove_payload(revision, impact, [resolution], key="idem_remove_incomplete")
        workflow_before = self.app._editor_version("campaign_alpha")
        with self.assertRaises(HTTPFailure) as caught:
            self.app.editor_record_remove("campaign_alpha", revision, "record-target", payload)
        self.assertEqual("incomplete_removal_resolution", caught.exception.payload["error"]["code"])
        self.assertEqual(revision, self.app.workflow.head("campaign_alpha"))
        self.assertEqual(workflow_before, self.app._editor_version("campaign_alpha"))

    def test_invalid_redirect_fails_before_claim_and_preserves_head(self):
        self.backend._create_record("record-target")
        revision = self.backend._create_record(
            "record-source",
            connections=[{"connection_id": "connection_one", "target_record_id": "record-target", "relationship": "related-to", "state": "current", "context": "One."}],
        )
        _, impact = self.app.editor_removal_impact("campaign_alpha", revision, "record-target")
        resolutions = [{"reference_id": impact["incoming_references"][0]["reference_id"], "action": "redirect", "replacement_target_record_id": "record-missing"}]
        payload = self._remove_payload(revision, impact, resolutions, key="idem_remove_invalid")
        with self.assertRaises(HTTPFailure) as caught:
            self.app.editor_record_remove("campaign_alpha", revision, "record-target", payload)
        self.assertEqual("unknown_connection_target", caught.exception.payload["error"]["code"])
        self.assertEqual(revision, self.app.workflow.head("campaign_alpha"))


if __name__ == "__main__":
    unittest.main()
