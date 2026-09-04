from __future__ import annotations

from copy import deepcopy
import unittest

from tests.hosted.http import test_editor_backend as _editor_backend
from warden_drydock.hosted.http.contracts import canonical_digest, request_digest_input
from warden_drydock.hosted.http.editor import document_digest, mutate_document, parse_document


class EditorSourcePreservationTests(unittest.TestCase):
    """Focused regressions for source-preserving editor publications."""

    def setUp(self):
        self.backend = _editor_backend.EditorBackendTests(
            "test_edit_is_exact_and_replay_does_not_advance_workflow"
        )
        self.backend.setUp()
        self.addCleanup(self.backend.doCleanups)
        self.app = self.backend.app

    def _edit_candidate(self, candidate: dict, *, key: str):
        revision = self.app.workflow.head("campaign_alpha")
        view = self.app.editor_record_read("campaign_alpha", revision, "campaign-main")[1]
        operation = {
            "contract_name": "editor_operation_request", "contract_version": 1,
            "request_id": f"request_{key}", "operation": "editor_record_edit",
            "idempotency_key": key, "payload_digest": "0" * 64,
            "expected_revision": revision, "expected_editor_workflow_version": 1,
            "subject_id": "campaign-main",
        }
        payload = {
            "contract_name": "editor_record_edit_request", "contract_version": 1,
            "operation_request": operation,
            "binding": {
                "campaign_id": "campaign_alpha", "base_revision": view["viewed_revision"],
                "record_id": "campaign-main", "record_digest": view["record"]["content_digest"],
                "expected_editor_workflow_version": 1,
            },
            "candidate": candidate,
        }
        operation["payload_digest"] = canonical_digest(request_digest_input(payload))
        return revision, candidate, self.app.editor_record_edit(
            "campaign_alpha", revision, "campaign-main", payload,
        )

    def test_multiple_section_edits_publish_reviewed_candidate_and_keep_history(self):
        revision = self.app.workflow.head("campaign_alpha")
        before = self.app._record("campaign_alpha", revision, "campaign-main")["content"]
        view = self.app.editor_record_read("campaign_alpha", revision, "campaign-main")[1]
        candidate = deepcopy(view["record"])
        candidate["sections"][0]["body"] = "\n".join(
            ["First line one", "First line two", "First line three", "First line four", "First line five", "First line six"]
        )
        candidate["sections"][1]["body"] = "New design target"
        candidate["content_digest"] = document_digest(candidate)

        _, reviewed_candidate, (status, proposal) = self._edit_candidate(
            candidate, key="idem_source_preservation"
        )
        self.assertEqual(201, status)
        self.assertEqual("passed", proposal["validation"]["status"])
        self.assertEqual(reviewed_candidate["sections"], proposal["diff"]["cards"][0]["after"]["sections"])

        _, published = self.backend._approve_editor(proposal)
        published_revision = published["published_revision"]["revision_id"]
        readback = self.app.editor_record_read(
            "campaign_alpha", published_revision, "campaign-main"
        )[1]
        self.assertEqual(reviewed_candidate["sections"], readback["record"]["sections"])
        self.assertEqual(
            reviewed_candidate["sections"],
            parse_document(
                self.app._record("campaign_alpha", published_revision, "campaign-main")["content"],
                "campaign-main", "campaign",
            )["sections"],
        )

        historical = self.app.editor_record_read("campaign_alpha", revision, "campaign-main")[1]
        self.assertTrue(historical["historical"])
        self.assertEqual(before, self.app._record("campaign_alpha", revision, "campaign-main")["content"])

    def test_trailing_newlines_survive_real_publication_readback(self):
        revision = self.app.workflow.head("campaign_alpha")
        view = self.app.editor_record_read("campaign_alpha", revision, "campaign-main")[1]
        candidate = deepcopy(view["record"])
        candidate["sections"][0]["body"] = "First\nsecond\nthird\n"
        candidate["sections"][1]["body"] = "Replacement second section.\n"
        candidate["content_digest"] = document_digest(candidate)

        _, reviewed_candidate, (status, proposal) = self._edit_candidate(
            candidate, key="idem_trailing_newline_publication"
        )
        self.assertEqual(201, status)
        self.assertEqual(reviewed_candidate["sections"], proposal["diff"]["cards"][0]["after"]["sections"])

        _, published = self.backend._approve_editor(proposal)
        published_revision = published["published_revision"]["revision_id"]
        readback = self.app.editor_record_read(
            "campaign_alpha", published_revision, "campaign-main"
        )[1]
        self.assertEqual(reviewed_candidate["sections"], readback["record"]["sections"])
        self.assertEqual(
            reviewed_candidate["sections"],
            parse_document(
                self.app._record("campaign_alpha", published_revision, "campaign-main")["content"],
                "campaign-main", "campaign",
            )["sections"],
        )

    def test_trailing_newlines_survive_multiple_crlf_sections_before_connections(self):
        source = (
            "---\r\n"
            "id: record-main\r\n"
            "type: npc\r\n"
            "name: Keeper\r\n"
            "status: draft\r\n"
            "visibility: warden\r\n"
            "---\r\n\r\n"
            "## First\r\nOld first.\r\n\r\n"
            "## Second\r\nOld second.\r\n\r\n"
            "## Empty\r\n\r\n"
            "## Connections\r\n\r\n"
        )
        candidate = parse_document(source, "record-main", "npc")
        candidate["sections"][0]["body"] = "First\nsecond\n"
        candidate["sections"][1]["body"] = "Replacement second section.\n"
        candidate["sections"][2]["body"] = ""
        candidate["content_digest"] = document_digest(candidate)

        result = mutate_document(source, candidate)

        self.assertEqual(candidate["sections"], parse_document(result, "record-main", "npc")["sections"])
        self.assertNotIn("\r\r\n", result)
        self.assertEqual(result.count("\r\n"), result.count("\n"))

    def test_replacing_different_length_sections_preserves_unrelated_bytes_and_connections(self):
        source = """---
id: record-main
type: npc
name: Keeper
status: draft
visibility: warden
---

## First
Original first.

## Second
Original second line one.
Original second line two.

## Third
Remove this section.

## Notes
Preserve this unrelated section.

<!-- Preserve this comment. -->

## Connections

"""
        candidate = parse_document(source, "record-main", "npc")
        candidate["sections"] = [
            {"section_id": "first", "body": "\n".join(f"First line {i}" for i in range(1, 7))},
            {"section_id": "second", "body": "New second"},
            {"section_id": "notes", "body": "Preserve this unrelated section.\n\n<!-- Preserve this comment. -->"},
        ]
        candidate["content_digest"] = document_digest(candidate)

        result = mutate_document(source, candidate)

        self.assertEqual(candidate["sections"], parse_document(result, "record-main", "npc")["sections"])
        self.assertIn("<!-- Preserve this comment. -->", result)
        self.assertIn("## Notes\nPreserve this unrelated section.", result)
        self.assertIn("## Connections", result)
        self.assertNotIn("## Third", result)

    def test_crlf_multiple_section_edits_restore_source_newline_convention(self):
        source = (
            "---\r\n"
            "id: record-main\r\n"
            "type: npc\r\n"
            "name: Keeper\r\n"
            "status: draft\r\n"
            "visibility: warden\r\n"
            "---\r\n\r\n"
            "## First\r\nOld first.\r\n\r\n"
            "## Second\r\nOld second.\r\n\r\n"
            "## Connections\r\n\r\n"
        )
        candidate = parse_document(source, "record-main", "npc")
        candidate["sections"][0]["body"] = "First A\nFirst B\nFirst C"
        candidate["sections"][1]["body"] = "New second"
        candidate["content_digest"] = document_digest(candidate)

        result = mutate_document(source, candidate)

        self.assertEqual(candidate["sections"], parse_document(result, "record-main", "npc")["sections"])
        self.assertNotIn("\r\r\n", result)
        self.assertEqual(result.count("\r\n"), result.count("\n"))


if __name__ == "__main__":
    unittest.main()
