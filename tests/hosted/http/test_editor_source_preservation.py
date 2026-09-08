from __future__ import annotations

from copy import deepcopy
import unittest

from tests.hosted.http import test_editor_backend as _editor_backend
from warden_drydock.hosted.http.contracts import canonical_digest, request_digest_input
from warden_drydock.hosted.http.editor import (
    document_digest,
    mutate_document,
    parse_document,
    serialize_document,
)


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
        self.assertEqual(before, proposal["diff"]["source_changes"][0]["before_source"])
        self.assertIn("First line one", proposal["diff"]["source_changes"][0]["after_source"])

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

    def test_section_carriage_returns_are_normalized_before_review_and_readback(self):
        revision = self.app.workflow.head("campaign_alpha")
        view = self.app.editor_record_read("campaign_alpha", revision, "campaign-main")[1]
        candidate = deepcopy(view["record"])
        candidate["sections"][0]["body"] = "First\r\nsecond\rThird"
        candidate["content_digest"] = document_digest(candidate)

        _, reviewed_candidate, (status, proposal) = self._edit_candidate(
            candidate, key="idem_section_carriage_returns"
        )
        self.assertEqual(201, status)
        normalized_candidate = proposal["diff"]["cards"][0]["after"]
        self.assertEqual("First\nsecond\nThird", normalized_candidate["sections"][0]["body"])
        self.assertNotEqual(reviewed_candidate["sections"], normalized_candidate["sections"])

        _, published = self.backend._approve_editor(proposal)
        published_revision = published["published_revision"]["revision_id"]
        readback = self.app.editor_record_read(
            "campaign_alpha", published_revision, "campaign-main"
        )[1]["record"]
        self.assertEqual(normalized_candidate["sections"], readback["sections"])

    def test_leading_blank_lines_survive_real_publication_readback(self):
        revision = self.app.workflow.head("campaign_alpha")
        view = self.app.editor_record_read("campaign_alpha", revision, "campaign-main")[1]
        candidate = deepcopy(view["record"])
        candidate["sections"][0]["body"] = "\nFirst line after the intentional blank."
        candidate["content_digest"] = document_digest(candidate)

        _, reviewed_candidate, (status, proposal) = self._edit_candidate(
            candidate, key="idem_leading_newline_publication"
        )
        self.assertEqual(201, status)
        _, published = self.backend._approve_editor(proposal)
        published_revision = published["published_revision"]["revision_id"]
        readback = self.app.editor_record_read(
            "campaign_alpha", published_revision, "campaign-main"
        )[1]
        self.assertEqual(reviewed_candidate["sections"], readback["record"]["sections"])

    def test_quoted_frontmatter_values_survive_real_publication_readback(self):
        revision = self.app.workflow.head("campaign_alpha")
        view = self.app.editor_record_read("campaign_alpha", revision, "campaign-main")[1]
        candidate = deepcopy(view["record"])
        candidate["displayed_name"] = 'Keeper "quoted" \\ path'
        next(field for field in candidate["fields"] if field["field_id"] == "system")["value"] = 'mothership "quoted" \\ path'
        candidate["content_digest"] = document_digest(candidate)

        _, reviewed_candidate, (status, proposal) = self._edit_candidate(
            candidate, key="idem_quoted_frontmatter_publication"
        )
        self.assertEqual(201, status)
        _, published = self.backend._approve_editor(proposal)
        published_revision = published["published_revision"]["revision_id"]
        readback = self.app.editor_record_read(
            "campaign_alpha", published_revision, "campaign-main"
        )[1]["record"]

        self.assertEqual(reviewed_candidate["displayed_name"], readback["displayed_name"])
        self.assertEqual(reviewed_candidate["fields"], readback["fields"])

    def test_frontmatter_values_with_surrounding_whitespace_survive_publication(self):
        revision = self.app.workflow.head("campaign_alpha")
        view = self.app.editor_record_read("campaign_alpha", revision, "campaign-main")[1]
        candidate = deepcopy(view["record"])
        candidate["displayed_name"] = " Keeper "
        next(field for field in candidate["fields"] if field["field_id"] == "system")["value"] = " mothership "
        candidate["content_digest"] = document_digest(candidate)

        _, reviewed_candidate, (status, proposal) = self._edit_candidate(
            candidate, key="idem_whitespace_frontmatter_publication"
        )
        self.assertEqual(201, status)
        _, published = self.backend._approve_editor(proposal)
        published_revision = published["published_revision"]["revision_id"]
        readback = self.app.editor_record_read(
            "campaign_alpha", published_revision, "campaign-main"
        )[1]["record"]

        self.assertEqual(reviewed_candidate["displayed_name"], readback["displayed_name"])
        self.assertEqual(reviewed_candidate["fields"], readback["fields"])

    def test_unicode_line_separators_survive_frontmatter_publication(self):
        revision = self.app.workflow.head("campaign_alpha")
        view = self.app.editor_record_read("campaign_alpha", revision, "campaign-main")[1]
        candidate = deepcopy(view["record"])
        candidate["displayed_name"] = "Keeper\u2028Line\u2029Next"
        next(field for field in candidate["fields"] if field["field_id"] == "system")["value"] = "adapter\u2028value\u2029tail"
        candidate["content_digest"] = document_digest(candidate)

        _, reviewed_candidate, (status, proposal) = self._edit_candidate(
            candidate, key="idem_unicode_line_separators"
        )
        self.assertEqual(201, status)
        _, published = self.backend._approve_editor(proposal)
        published_revision = published["published_revision"]["revision_id"]
        readback = self.app.editor_record_read(
            "campaign_alpha", published_revision, "campaign-main"
        )[1]["record"]
        self.assertEqual(reviewed_candidate["displayed_name"], readback["displayed_name"])
        self.assertEqual(reviewed_candidate["fields"], readback["fields"])

    def test_typed_frontmatter_scalars_round_trip_through_source_preserving_mutation(self):
        source = """---
id: record-main
type: npc
name: Keeper
status: draft
visibility: warden
count: old
ratio: old
enabled: old
empty: old
text_number: old
---

## Summary
Keep this record.
"""
        candidate = parse_document(source, "record-main", "npc")
        typed_values = {
            "count": 42,
            "ratio": 3.5,
            "enabled": True,
            "empty": None,
            "text_number": "42",
        }
        for field in candidate["fields"]:
            if field["field_id"] in typed_values:
                field["value"] = typed_values[field["field_id"]]
        candidate["content_digest"] = document_digest(candidate)

        result = mutate_document(source, candidate)
        round_tripped = parse_document(result, "record-main", "npc")

        self.assertEqual(typed_values, {field["field_id"]: field["value"] for field in round_tripped["fields"]})

    def test_typed_frontmatter_scalars_survive_proposal_approval_and_readback(self):
        for index, value in enumerate((42, 3.5, True, None, "42")):
            backend = _editor_backend.EditorBackendTests(
                "test_edit_is_exact_and_replay_does_not_advance_workflow"
            )
            backend.setUp()
            self.addCleanup(backend.doCleanups)
            self.backend = backend
            self.app = backend.app
            revision = self.app.workflow.head("campaign_alpha")
            view = self.app.editor_record_read("campaign_alpha", revision, "campaign-main")[1]
            candidate = deepcopy(view["record"])
            next(field for field in candidate["fields"] if field["field_id"] == "system")["value"] = value
            candidate["content_digest"] = document_digest(candidate)

            _, reviewed_candidate, (status, proposal) = self._edit_candidate(
                candidate, key=f"idem_typed_frontmatter_publication_{index}"
            )
            self.assertEqual(201, status)
            _, published = backend._approve_editor(proposal)
            published_revision = published["published_revision"]["revision_id"]
            readback = self.app.editor_record_read(
                "campaign_alpha", published_revision, "campaign-main"
            )[1]["record"]

            self.assertEqual(reviewed_candidate["fields"], readback["fields"])

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

    def test_crlf_frontmatter_fields_survive_editor_mutation(self):
        source = (
            "---\r\n"
            "id: record-main\r\n"
            "type: npc\r\n"
            "name: Keeper\r\n"
            "status: draft\r\n"
            "visibility: warden\r\n"
            "system: mothership\r\n"
            "ownership: campaign\r\n"
            "---\r\n\r\n"
            "## Summary\r\nKeep this record.\r\n"
        )
        candidate = parse_document(source, "record-main", "npc")
        candidate["displayed_name"] = "Updated Keeper"
        candidate["content_digest"] = document_digest(candidate)

        result = mutate_document(source, candidate)
        round_tripped = parse_document(result, "record-main", "npc")

        self.assertEqual(candidate["fields"], round_tripped["fields"])
        self.assertEqual("Updated Keeper", round_tripped["displayed_name"])
        self.assertNotIn("\r\r\n", result)
        self.assertEqual(result.count("\r\n"), result.count("\n"))

    def test_section_bodies_survive_creation_serialization(self):
        candidate = {
            "record_id": "record-main",
            "record_type": "npc",
            "displayed_name": "Keeper",
            "status": "draft",
            "authority": "preparation",
            "visibility": {"audience": "warden", "warden_only": True},
            "fields": [],
            "sections": [
                {"section_id": "first", "body": "First body."},
                {"section_id": "second", "body": "Second body.\n"},
            ],
            "connections": [],
            "content_digest": "0" * 64,
        }
        candidate["content_digest"] = document_digest(candidate)

        serialized = serialize_document(candidate)

        self.assertEqual(candidate["sections"], parse_document(serialized, "record-main", "npc")["sections"])

    def test_new_connections_section_preserves_occurrence_ids(self):
        source = """---
id: record-main
type: npc
name: Keeper
status: draft
visibility: warden
---

## Summary
Keep this record.
"""
        candidate = parse_document(source, "record-main", "npc")
        candidate["connections"] = [{
            "connection_id": "custom_occurrence",
            "target_record_id": "record-gate",
            "relationship": "guards",
            "state": "current",
            "context": "Watches the gate.",
        }]
        candidate["content_digest"] = document_digest(candidate)

        result = mutate_document(source, candidate)
        round_tripped = parse_document(result, "record-main", "npc")

        self.assertIn("<!-- drydock:connection-id=custom_occurrence -->", result)
        self.assertEqual(candidate["connections"], round_tripped["connections"])

    def test_mutating_connection_preserves_unrelated_markdown_bullets(self):
        source = """---
id: record-main
type: npc
name: Keeper
status: draft
visibility: warden
---

## Connections

- An ordinary Markdown note.
<!-- drydock:connection-id=custom_occurrence -->
- `guards` -> [[record-gate]] (`current`) — Watches the gate.
"""
        candidate = parse_document(source, "record-main", "npc")
        candidate["connections"][0]["context"] = "Watches the gate quietly."
        candidate["content_digest"] = document_digest(candidate)

        result = mutate_document(source, candidate)

        self.assertIn("- An ordinary Markdown note.", result)
        self.assertEqual(candidate["connections"], parse_document(result, "record-main", "npc")["connections"])
        self.assertIn("<!-- drydock:connection-id=custom_occurrence -->", result)

    def test_duplicate_connections_heading_preserves_non_typed_content(self):
        source = """---
id: record-main
type: npc
name: Keeper
status: draft
visibility: warden
---

## Connections

- `guards` -> [[record-gate]] (`current`) — Watches the gate.

## Connections

Duplicate prose stays here.
<!-- Preserve this duplicate comment. -->
- An ordinary duplicate bullet stays here.
- `visits` -> [[record-hall]] (`current`) — Checks in.

## Notes
Keep this section.
"""
        candidate = parse_document(source, "record-main", "npc")
        candidate["displayed_name"] = "Keeper Updated"
        candidate["content_digest"] = document_digest(candidate)

        result = mutate_document(source, candidate)

        self.assertEqual(1, result.count("## Connections"))
        self.assertIn("Duplicate prose stays here.", result)
        self.assertIn("<!-- Preserve this duplicate comment. -->", result)
        self.assertIn("- An ordinary duplicate bullet stays here.", result)
        self.assertNotIn("visits", result)
        self.assertIn("## Notes\nKeep this section.", result)

    def test_interleaved_non_typed_content_keeps_original_position(self):
        source = """---
id: record-main
type: npc
name: Keeper
status: draft
visibility: warden
---

## Connections

- `guards` -> [[record-gate]] (`current`) — Watches the gate.
Campaign-authored prose remains between these connections.
- An ordinary Markdown bullet remains between these connections.
- `visits` -> [[record-hall]] (`current`) — Checks in.
"""
        candidate = parse_document(source, "record-main", "npc")
        candidate["connections"][0]["context"] = "Watches the gate quietly."
        candidate["connections"][1]["context"] = "Checks in at dusk."
        candidate["content_digest"] = document_digest(candidate)

        result = mutate_document(source, candidate)

        first = result.index("Watches the gate quietly.")
        prose = result.index("Campaign-authored prose remains between these connections.")
        bullet = result.index("- An ordinary Markdown bullet remains between these connections.")
        second = result.index("Checks in at dusk.")
        self.assertLess(first, prose)
        self.assertLess(prose, bullet)
        self.assertLess(bullet, second)
        self.assertEqual(candidate["connections"], parse_document(result, "record-main", "npc")["connections"])

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

    def test_serialized_name_and_connection_context_round_trip(self):
        source = """---
id: record-main
type: npc
name: Keeper
status: draft
visibility: warden
---

## Summary
Keep this record.

## Connections

- `connected-to` -> [[record-gate]] (`current`) — Watches the gate.
"""
        candidate = parse_document(source, "record-main", "npc")
        candidate["displayed_name"] = "Keeper: Alpha"
        candidate["connections"][0]["context"] = "Watches the gate: quietly."
        candidate["content_digest"] = document_digest(candidate)

        serialized = serialize_document(candidate)
        round_tripped = parse_document(serialized, "record-main", "npc")

        self.assertEqual(candidate["displayed_name"], round_tripped["displayed_name"])
        self.assertEqual(candidate["connections"], round_tripped["connections"])

    def test_removed_connection_keeps_surviving_occurrence_id(self):
        source = """---
id: record-main
type: npc
name: Keeper
status: draft
visibility: warden
---

## Summary
Keep this record.

## Connections

- `guards` -> [[record-gate]] (`current`) — Watches the gate.
- `visits` -> [[record-hall]] (`current`) — Visits the hall.
"""
        candidate = parse_document(source, "record-main", "npc")
        candidate["connections"] = candidate["connections"][1:]
        candidate["content_digest"] = document_digest(candidate)

        result = mutate_document(source, candidate)
        round_tripped = parse_document(result, "record-main", "npc")

        self.assertEqual("connection_2", candidate["connections"][0]["connection_id"])
        self.assertEqual(candidate["connections"], round_tripped["connections"])

    def test_serialized_name_and_connection_context_reject_line_breaks(self):
        source = """---
id: record-main
type: npc
name: Keeper
status: draft
visibility: warden
---

## Summary
Keep this record.

## Connections

- `connected-to` -> [[record-gate]] (`current`) — Watches the gate.
"""
        candidate = parse_document(source, "record-main", "npc")

        candidate["displayed_name"] = "Keeper\nInjected"
        candidate["content_digest"] = document_digest(candidate)
        with self.assertRaisesRegex(ValueError, "invalid_record_name"):
            serialize_document(candidate)

        for boundary in ("\r\n", "\u2028", "\u2029", "\u0085", "\v", "\f", "\x1c", "\x1d", "\x1e"):
            candidate = parse_document(source, "record-main", "npc")
            candidate["connections"][0]["context"] = f"Watches the gate{boundary}forged"
            candidate["content_digest"] = document_digest(candidate)
            with self.assertRaisesRegex(ValueError, "invalid_connection_context"):
                serialize_document(candidate)

        candidate = parse_document(source, "record-main", "npc")
        candidate["connections"][0]["context"] = " Watches the gate."
        candidate["content_digest"] = document_digest(candidate)
        with self.assertRaisesRegex(ValueError, "invalid_connection_context"):
            serialize_document(candidate)


if __name__ == "__main__":
    unittest.main()
