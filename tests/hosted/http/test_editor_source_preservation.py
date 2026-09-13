from __future__ import annotations

from copy import deepcopy
import json
import shutil
import unittest
from pathlib import Path
from unittest import mock

from tests.hosted.http import test_editor_backend as _editor_backend
from warden_drydock.hosted.http.contracts import canonical_digest, request_digest_input
from warden_drydock.core.generator import DATA
from warden_drydock.hosted.http.editor import (
    adapter_editor_definition,
    document_digest,
    mutate_document,
    parse_document,
    serialize_document,
    validate_adapter_document,
)
from warden_drydock.hosted.http.editor_semantics import _property_changes
from warden_drydock.standalone import parse_connections


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

    def test_section_reordering_is_rejected_before_source_mutation(self):
        source = """---
id: record-main
type: npc
name: Keeper
status: draft
visibility: warden
---

## Summary
Keep this record.

## Wants
Keep these wants.
"""
        before = parse_document(source, "record-main", "npc")
        candidate = deepcopy(before)
        candidate["sections"] = [candidate["sections"][1], candidate["sections"][0]]

        with self.assertRaisesRegex(ValueError, "editor_section_reordering_not_allowed"):
            validate_adapter_document(candidate, adapter_editor_definition("mothership"), before)

    def test_supported_fields_and_sections_can_be_removed(self):
        source = """---
id: record-main
type: npc
name: Keeper
status: draft
visibility: warden
score: 1
---

## Summary
Keep this record.

## Notes
Remove these notes.
"""
        before = parse_document(source, "record-main", "npc")
        candidate = deepcopy(before)
        candidate["fields"] = [item for item in candidate["fields"] if item["field_id"] != "score"]
        candidate["sections"] = [item for item in candidate["sections"] if item["section_id"] != "notes"]
        candidate["content_digest"] = document_digest(candidate)
        definition = {
            "records": {"npc": {
                "fields": {"score"}, "sections": {"summary", "notes"},
                "required_fields": set(), "nonempty_fields": set(),
                "required_values": {}, "forbidden_headings": set(),
            }},
            "creatable": {"npc"}, "relationships": set(), "states": set(),
        }

        validate_adapter_document(candidate, definition, before)
        result = mutate_document(source, candidate)

        self.assertNotIn("score:", result)
        self.assertNotIn("## Notes", result)
        self.assertEqual(candidate, parse_document(result, "record-main", "npc"))

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

    def test_unicode_line_separator_does_not_create_a_mutation_heading(self):
        source = """---
id: record-main
type: npc
name: Keeper
status: draft
visibility: warden
---

## Summary
Keep this record.\u2028## This resembles a heading
This remains part of the summary.

## Notes
Keep this section.
"""
        candidate = parse_document(source, "record-main", "npc")
        candidate["displayed_name"] = "Keeper Updated"
        candidate["content_digest"] = document_digest(candidate)

        result = mutate_document(source, candidate)
        round_tripped = parse_document(result, "record-main", "npc")

        self.assertEqual(candidate["sections"], round_tripped["sections"])
        self.assertIn("Keep this record.\u2028## This resembles a heading\n", result)
        self.assertIn("This remains part of the summary.", result)

    def test_duplicate_normalized_headings_have_unique_ids_and_keep_source_blocks(self):
        source = """---
id: record-main
type: npc
name: Keeper
status: draft
visibility: warden
---

## Summary
First authored block.

## SUMMARY
Second authored block.
"""
        candidate = parse_document(source, "record-main", "npc")

        self.assertEqual(["summary", "summary-2"], [item["section_id"] for item in candidate["sections"]])

        candidate["displayed_name"] = "Updated Keeper"
        candidate["sections"][1]["body"] = "Updated second authored block."
        candidate["content_digest"] = document_digest(candidate)
        result = mutate_document(source, candidate)

        self.assertIn("## Summary\nFirst authored block.", result)
        self.assertIn("## SUMMARY\nUpdated second authored block.", result)
        self.assertEqual(candidate["sections"], parse_document(result, "record-main", "npc")["sections"])

    def test_editor_record_read_accepts_duplicate_normalized_headings(self):
        source = """---
id: campaign-main
type: campaign
name: Editor
status: draft
visibility: warden
---

## Summary
First authored block.

## SUMMARY
Second authored block.
"""
        revision = self.app.workflow.head("campaign_alpha")
        record = {"content": source, "record_type": "campaign"}

        with mock.patch.object(self.app, "_record", return_value=record):
            status, response = self.app.editor_record_read("campaign_alpha", revision, "campaign-main")

        self.assertEqual(200, status)
        self.assertEqual(
            ["summary", "summary-2"],
            [item["section_id"] for item in response["record"]["sections"]],
        )

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

    def test_structured_and_non_finite_field_values_fail_before_proposal_creation(self):
        revision = self.app.workflow.head("campaign_alpha")
        view = self.app.editor_record_read("campaign_alpha", revision, "campaign-main")[1]
        source_before = self.app._record("campaign_alpha", revision, "campaign-main")["content"]

        for index, value in enumerate(({"unexpected": "object"}, ["unexpected"], float("nan"), float("inf"), float("-inf"))):
            candidate = deepcopy(view["record"])
            candidate["fields"][0]["value"] = value
            candidate["content_digest"] = document_digest(candidate)
            with self.subTest(value=value):
                with self.assertRaises(_editor_backend.HTTPFailure) as caught:
                    self._edit_candidate(candidate, key=f"idem_invalid_field_value_{index}")
                self.assertEqual("invalid_field_value", caught.exception.payload["error"]["code"])
                self.assertEqual(revision, self.app.workflow.head("campaign_alpha"))
                self.assertEqual(source_before, self.app._record("campaign_alpha", revision, "campaign-main")["content"])
                self.assertEqual({}, self.app._editor_proposals)

    def test_connection_removal_matches_surviving_rows_by_occurrence_id(self):
        source = (
            "---\n"
            "id: record-source\n"
            "type: npc\n"
            "name: Source\n"
            "status: draft\n"
            "visibility: warden\n"
            "---\n\n"
            "## Summary\n"
            "Keep this record.\n\n"
            "## Connections\n\n"
            "<!-- drydock:connection-id=connection_first -->\n"
            "- `connected-to` -> [[record-first]] (`current`) — First.\n"
            "Campaign-authored prose between connections.\n"
            "<!-- drydock:connection-id=connection_second -->\n"
            "- `supports` -> [[record-second]] (`current`) — Second.\n"
        )
        candidate = parse_document(source, "record-source", "npc")
        candidate["connections"] = [candidate["connections"][1]]
        candidate["content_digest"] = document_digest(candidate)

        result = mutate_document(source, candidate)

        self.assertNotIn("record-first", result)
        self.assertIn("Campaign-authored prose between connections.", result)
        self.assertLess(result.index("Campaign-authored prose"), result.index("connection_second"))
        self.assertEqual(candidate["connections"], parse_document(result, "record-source", "npc")["connections"])

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

    def test_numeric_scalar_type_changes_are_preserved_during_mutation(self):
        source = """---
id: record-main
type: npc
name: Keeper
status: draft
visibility: warden
score: 1.5
enabled: true
ratio: 1.5
---

## Summary
Keep this record.
"""
        candidate = parse_document(source, "record-main", "npc")
        for field in candidate["fields"]:
            if field["field_id"] == "score":
                field["value"] = 1
            elif field["field_id"] == "enabled":
                field["value"] = 1
        candidate["content_digest"] = document_digest(candidate)

        property_changes = self.app._editor_property_changes(
            parse_document(source, "record-main", "npc"), candidate,
        )
        self.assertIn(
            {"property": "fields.score", "before": 1.5, "after": 1},
            property_changes,
        )
        self.assertIn(
            {"property": "fields.score", "before": 1.5, "after": 1},
            _property_changes(parse_document(source, "record-main", "npc"), candidate),
        )

        result = mutate_document(source, candidate)
        round_tripped = parse_document(result, "record-main", "npc")
        values = {field["field_id"]: field["value"] for field in round_tripped["fields"]}

        self.assertEqual(1, values["score"])
        self.assertIs(type(values["score"]), int)
        self.assertEqual(1, values["enabled"])
        self.assertIs(type(values["enabled"]), int)
        self.assertEqual(1.5, values["ratio"])
        self.assertIs(type(values["ratio"]), float)

    def test_unheaded_body_is_not_repeated_when_metadata_changes(self):
        source = """---
id: record-main
type: npc
name: Keeper
status: draft
visibility: warden
---

Campaign-authored prose without a heading.
It must remain in place.
"""
        candidate = parse_document(source, "record-main", "npc")
        candidate["displayed_name"] = "Updated Keeper"
        candidate["content_digest"] = document_digest(candidate)

        result = mutate_document(source, candidate)

        self.assertNotIn("## summary", result.casefold())
        self.assertEqual(1, result.count("Campaign-authored prose without a heading."))
        self.assertIn("name: Updated Keeper\n", result)
        self.assertIn("warden_only: true\n", result)
        self.assertTrue(result.endswith("Campaign-authored prose without a heading.\nIt must remain in place.\n"))

    def test_no_frontmatter_edit_preserves_prose_before_first_heading(self):
        source = (
            "Campaign-authored introduction.\n"
            "It must remain before the typed sections.\n\n"
            "## Summary\n"
            "Original summary.\n"
        )
        candidate = parse_document(source, "record-main", "npc")
        candidate["displayed_name"] = "Updated Keeper"
        candidate["sections"][0]["body"] = "Updated summary."
        candidate["content_digest"] = document_digest(candidate)

        result = mutate_document(source, candidate)

        self.assertIn("Campaign-authored introduction.\nIt must remain before the typed sections.\n\n", result)
        self.assertIn("## Summary\nUpdated summary.\n", result)

    def test_one_crlf_does_not_convert_an_lf_source_to_crlf(self):
        source = (
            "---\n"
            "id: record-main\n"
            "type: npc\n"
            "name: Keeper\n"
            "status: draft\n"
            "visibility: warden\n"
            "---\n\n"
            "## Summary\nOriginal\r\n"
        )
        candidate = parse_document(source, "record-main", "npc")
        candidate["displayed_name"] = "Updated Keeper"
        candidate["content_digest"] = document_digest(candidate)

        result = mutate_document(source, candidate)

        self.assertNotIn("\r\n", result)

    def test_editor_rejects_integer_fields_outside_javascript_safe_range(self):
        source = """---
id: record-main
type: npc
name: Keeper
status: draft
visibility: warden
count: 9007199254740992
---

## Summary
Keep this record.
"""

        with self.assertRaisesRegex(ValueError, "invalid_field_value"):
            parse_document(source, "record-main", "npc")

        revision = self.app.workflow.head("campaign_alpha")
        with mock.patch.object(self.app, "_record", return_value={"content": source, "record_type": "npc"}):
            with self.assertRaises(_editor_backend.HTTPFailure) as caught:
                self.app.editor_record_read("campaign_alpha", revision, "record-main")
        self.assertEqual((422, "record_not_editable"), (
            caught.exception.status, caught.exception.payload["error"]["code"],
        ))

    def test_editor_parse_failures_are_structured_across_read_and_impact_paths(self):
        malformed = [
            "---\nid: record-main\ntype: npc\nname: Keeper\nstatus: draft\nvisibility: yes\n---\n",
            "---\nid: record-main\ntype: npc\nname: \nstatus: draft\nvisibility: warden\n---\n",
            "---\nid: record-main\ntype: npc\nname: Keeper\nstatus: unknown\nvisibility: warden\n---\n",
        ]
        revision = self.app.workflow.head("campaign_alpha")
        for source in malformed:
            with self.subTest(source=source), mock.patch.object(
                self.app, "_record", return_value={"content": source, "record_type": "npc"}
            ):
                with self.assertRaises(_editor_backend.HTTPFailure) as caught:
                    self.app.editor_record_read("campaign_alpha", revision, "record-main")
            self.assertEqual((422, "record_not_editable"), (
                caught.exception.status, caught.exception.payload["error"]["code"],
            ))

        with mock.patch.object(
            self.app, "_record", return_value={"content": malformed[0], "record_type": "npc"}
        ), mock.patch.object(self.app, "_editor_record_ids", return_value={"record-main"}):
            with self.assertRaises(_editor_backend.HTTPFailure) as caught:
                self.app.editor_removal_impact("campaign_alpha", revision, "record-main")
        self.assertEqual((422, "record_not_editable"), (
            caught.exception.status, caught.exception.payload["error"]["code"],
        ))

    def test_malformed_nested_editor_binding_fails_as_422(self):
        revision, payload, _ = self.backend._edit("idem_malformed_nested_binding")
        payload["operation_request"]["idempotency_key"] = "idem_malformed_nested_binding_retry"
        payload["operation_request"]["expected_editor_workflow_version"] = 2
        payload["binding"]["expected_editor_workflow_version"] = 2
        payload["binding"]["base_revision"] = "stale"
        payload["operation_request"]["payload_digest"] = self.app._editor_payload_digest(payload)

        with self.assertRaises(_editor_backend.HTTPFailure) as caught:
            self.app.editor_record_edit("campaign_alpha", revision, "campaign-main", payload)
        self.assertEqual((422, "invalid_editor_binding"), (
            caught.exception.status, caught.exception.payload["error"]["code"],
        ))

        with self.assertRaises(_editor_backend.HTTPFailure) as caught:
            self.app._editor_validate_resolution_actions(
                {"resolutions": [{"action": "redirect"}]}, "editor_proposal"
            )
        self.assertEqual((422, "invalid_resolution_shape"), (
            caught.exception.status, caught.exception.payload["error"]["code"],
        ))

    def test_editor_normalizes_integral_float_fields_within_javascript_safe_range(self):
        source = """---
id: record-main
type: npc
name: Keeper
status: draft
visibility: warden
count: 1.0
---

## Summary
Keep this record.
"""

        document = parse_document(source, "record-main", "npc")
        self.assertEqual(1, document["fields"][0]["value"])
        self.assertIs(type(document["fields"][0]["value"]), int)

    def test_custom_frontmatter_keys_survive_typed_mutation(self):
        source = """---
id: record-main
type: npc
name: Keeper
status: draft
visibility: warden
Custom Note: Keep this authored metadata.
---

## Summary
Keep this record.
"""
        candidate = parse_document(source, "record-main", "npc")
        self.assertNotIn("Custom Note", {field["field_id"] for field in candidate["fields"]})
        candidate["displayed_name"] = "Updated Keeper"
        candidate["content_digest"] = document_digest(candidate)

        result = mutate_document(source, candidate)

        self.assertIn("Custom Note: Keep this authored metadata.\n", result)
        self.assertEqual("Updated Keeper", parse_document(result, "record-main", "npc")["displayed_name"])

    def test_project_definitions_are_loaded_from_bound_revision(self):
        revision_root = Path(self.backend.tmp.name) / "revision"
        shutil.copytree(DATA / "adapters" / "mothership", revision_root)
        project_template = revision_root / "01-campaign" / "campaign-overview.md"
        project_template.parent.mkdir(parents=True, exist_ok=True)
        project_template.write_text("""---
id: campaign-main
type: campaign
status: draft
ownership: campaign
name: \"{{campaign_name}}\"
bound_revision_field: \"from revision\"
---

## Bound section
""", encoding="utf-8")
        legacy_template = revision_root / "01-campaign" / "legacy-overview.md"
        legacy_template.write_text("""---
type: legacy-campaign
status: draft
ownership: campaign
legacy_field: from old revision
---

## Legacy section
""", encoding="utf-8")
        (revision_root / ".drydock-lock.json").write_text(json.dumps({
            "schema_version": 1,
            "files": {
                "01-campaign/legacy-overview.md": {"ownership": "campaign"},
            },
        }), encoding="utf-8")

        definition = adapter_editor_definition("mothership", revision_root)

        self.assertIn("bound_revision_field", definition["records"]["campaign"]["fields"])
        self.assertNotIn("system", definition["records"]["campaign"]["fields"])
        self.assertIn("legacy_field", definition["records"]["legacy-campaign"]["fields"])

    def test_unsupported_adapter_fields_use_typed_equality(self):
        before = {
            "record_type": "npc",
            "fields": [{"field_id": "score", "value": 1.0}],
            "sections": [],
            "connections": [],
        }
        candidate = {
            "record_type": "npc",
            "fields": [{"field_id": "score", "value": 1}],
            "sections": [],
            "connections": [],
        }
        definition = {
            "records": {"npc": {"fields": set(), "sections": set()}},
            "creatable": {"npc"},
            "relationships": set(),
            "states": set(),
        }

        with self.assertRaisesRegex(ValueError, "unsupported_editor_fields"):
            validate_adapter_document(candidate, definition, before)

    def test_nonempty_adapter_fields_accept_non_string_scalars(self):
        candidate = {
            "record_id": "record-main",
            "record_type": "npc",
            "displayed_name": "Keeper",
            "status": "draft",
            "visibility": {"audience": "warden", "warden_only": True},
            "fields": [
                {"field_id": "score", "value": 0},
                {"field_id": "enabled", "value": False},
            ],
            "sections": [],
            "connections": [],
        }
        definition = {
            "records": {"npc": {
                "fields": {"score", "enabled"}, "sections": set(),
                "required_fields": set(), "nonempty_fields": {"score", "enabled"},
                "required_values": {}, "forbidden_headings": set(),
            }},
            "creatable": {"npc"}, "relationships": set(), "states": set(),
        }

        validate_adapter_document(candidate, definition, None)

    def test_source_connection_trailing_whitespace_is_read_as_context_only(self):
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

""" + "- `guards` -> [[record-gate]] (`current`) — Watches the gate.  \n"
        candidate = parse_document(source, "record-main", "npc")
        self.assertEqual("Watches the gate.", candidate["connections"][0]["context"])

        candidate["displayed_name"] = "Updated Keeper"
        candidate["content_digest"] = document_digest(candidate)
        result = mutate_document(source, candidate)
        self.assertIn("— Watches the gate.  \n", result)

        candidate["connections"][0]["context"] = "Watches the gate.  "
        candidate["content_digest"] = document_digest(candidate)
        with self.assertRaisesRegex(ValueError, "invalid_connection_context"):
            serialize_document(candidate)

    def test_parse_connections_normalizes_crlf_and_bare_cr_line_terminators(self):
        source = (
            "## Connections\r\n\r\n"
            "- `guards` -> [[record-gate]] (`current`) — Watches the gate.\r"
            "- `supports` -> [[record-hall]] (`current`) — Checks the hall.\r\n"
        )

        connections, errors = parse_connections(
            source, source_id="record-main", path=Path("record-main.md")
        )

        self.assertEqual([], errors)
        self.assertEqual(["record-gate", "record-hall"], [item.target_id for item in connections])
        self.assertEqual([3, 4], [item.line for item in connections])

    def test_public_connection_ids_require_three_characters(self):
        candidate = {
            "record_id": "record-main",
            "record_type": "npc",
            "displayed_name": "Keeper",
            "status": "draft",
            "authority": "preparation",
            "visibility": {"audience": "warden", "warden_only": True},
            "fields": [],
            "sections": [{"section_id": "summary", "body": "Keep this record."}],
            "connections": [{
                "connection_id": "a",
                "target_record_id": "record-gate",
                "relationship": "guards",
                "state": "current",
                "context": "Watches the gate.",
            }],
            "content_digest": "0" * 64,
        }
        candidate["content_digest"] = document_digest(candidate)

        with self.assertRaisesRegex(ValueError, "unsafe_identifier"):
            serialize_document(candidate)

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

    def test_missing_sections_use_bound_adapter_labels_during_source_mutation(self):
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
        candidate["sections"].append({"section_id": "current-state", "body": "Ready for review.\n"})
        candidate["content_digest"] = document_digest(candidate)

        result = mutate_document(
            source,
            candidate,
            {"current-state": "Current state"},
        )

        self.assertIn("## Current state\nReady for review.\n", result)
        self.assertNotIn("## current-state", result)
        self.assertEqual(candidate["sections"], parse_document(result, "record-main", "npc")["sections"])

    def test_long_authored_headings_use_stable_bounded_section_ids(self):
        heading = "A " + ("very long authored heading " * 5)
        source = f"""---
id: record-main
type: npc
name: Keeper
status: draft
visibility: warden
---

## {heading}
Keep this record.
"""
        first = parse_document(source, "record-main", "npc")
        second = parse_document(source, "record-main", "npc")

        self.assertEqual(first["sections"], second["sections"])
        self.assertLessEqual(len(first["sections"][0]["section_id"]), 80)
        self.assertNotEqual(heading.lower().replace(" ", "-"), first["sections"][0]["section_id"])

        first["sections"][0]["body"] = "Updated record."
        first["content_digest"] = document_digest(first)
        result = mutate_document(source, first)
        self.assertEqual(first["sections"], parse_document(result, "record-main", "npc")["sections"])

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

    def test_duplicate_connection_markers_get_unique_occurrence_ids(self):
        source = """---
id: record-main
type: npc
name: Keeper
status: draft
visibility: warden
---

## Connections

<!-- drydock:connection-id=duplicate_marker -->
- `guards` -> [[record-gate]] (`current`) — Watches the gate.
<!-- drydock:connection-id=duplicate_marker -->
- `supports` -> [[record-hall]] (`current`) — Checks the hall.
"""
        candidate = parse_document(source, "record-main", "npc")

        self.assertEqual(
            ["duplicate_marker", "duplicate_marker_2"],
            [item["connection_id"] for item in candidate["connections"]],
        )

        candidate["connections"][1]["context"] = "Checks the hall carefully."
        candidate["content_digest"] = document_digest(candidate)
        result = mutate_document(source, candidate)

        self.assertIn("<!-- drydock:connection-id=duplicate_marker -->", result)
        self.assertIn("<!-- drydock:connection-id=duplicate_marker_2 -->", result)
        self.assertEqual(candidate["connections"], parse_document(result, "record-main", "npc")["connections"])

    def test_long_connection_markers_are_bounded_and_stable(self):
        marker = "marker_" + ("a" * 90)
        source = f"""---
id: record-main
type: npc
name: Keeper
status: draft
visibility: warden
---

## Connections

<!-- drydock:connection-id={marker} -->
- `guards` -> [[record-gate]] (`current`) — Watches the gate.
"""
        first = parse_document(source, "record-main", "npc")
        second = parse_document(source, "record-main", "npc")

        self.assertEqual(first["connections"], second["connections"])
        self.assertLessEqual(len(first["connections"][0]["connection_id"]), 80)
        self.assertNotEqual(marker, first["connections"][0]["connection_id"])

    def test_mutation_slots_reuse_collision_safe_fallback_ids(self):
        source = """---
id: record-main
type: npc
name: Keeper
status: draft
visibility: warden
---

## Connections

<!-- drydock:connection-id=connection_2 -->
- `guards` -> [[record-gate]] (`current`) — Watches the gate.
- `supports` -> [[record-hall]] (`current`) — Checks the hall.
"""
        candidate = parse_document(source, "record-main", "npc")
        self.assertEqual(["connection_2", "connection_2_2"], [
            item["connection_id"] for item in candidate["connections"]
        ])
        candidate["connections"][1]["context"] = "Checks the hall carefully."
        candidate["content_digest"] = document_digest(candidate)

        result = mutate_document(source, candidate)

        self.assertEqual(candidate["connections"], parse_document(result, "record-main", "npc")["connections"])

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

    def test_mutating_connection_preserves_unchanged_source_formatting(self):
        source = """---
id: record-main
type: npc
name: Keeper
status: draft
visibility: warden
---

## Connections

<!-- drydock:connection-id=connection_1 -->
- `guards` -> [[record-gate|Gate]] (`current`) — Watches the gate.
<!-- drydock:connection-id=connection_2 -->
- `supports` -> [[record-hall|Hall]] (`current`) — Checks the hall.
"""
        candidate = parse_document(source, "record-main", "npc")
        candidate["connections"][1]["context"] = "Checks the hall carefully."
        candidate["content_digest"] = document_digest(candidate)

        result = mutate_document(source, candidate)

        self.assertIn("- `guards` -> [[record-gate|Gate]] (`current`) — Watches the gate.", result)
        self.assertIn("- `supports` -> [[record-hall]] (`current`) — Checks the hall carefully.", result)

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

        self.assertEqual(2, result.count("## Connections"))
        self.assertIn("Duplicate prose stays here.", result)
        self.assertIn("<!-- Preserve this duplicate comment. -->", result)
        self.assertIn("- An ordinary duplicate bullet stays here.", result)
        self.assertIn("- `visits` -> [[record-hall]] (`current`) — Checks in.", result)
        self.assertIn("## Notes\nKeep this section.", result)
        connections, errors = parse_connections(
            result, source_id="record-main", path=Path("record-main.md")
        )
        self.assertEqual(["record-gate"], [item.target_id for item in connections])
        self.assertEqual([], errors)

    def test_duplicate_connections_heading_preserves_paragraph_content(self):
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

Authored paragraph stays here.
It remains a paragraph under the duplicate heading.

## Notes
Keep this section.
"""
        candidate = parse_document(source, "record-main", "npc")
        candidate["displayed_name"] = "Keeper Updated"
        candidate["content_digest"] = document_digest(candidate)

        result = mutate_document(source, candidate)

        self.assertEqual(2, result.count("## Connections"))
        self.assertIn(
            "## Connections\n\nAuthored paragraph stays here.\n"
            "It remains a paragraph under the duplicate heading.",
            result,
        )

    def test_first_connection_is_inserted_after_authored_paragraph(self):
        source = """---
id: record-main
type: npc
name: Keeper
status: draft
visibility: warden
---

## Connections

Authored paragraph stays intact.
Its second line remains together.

## Notes
Keep this section.
"""
        candidate = parse_document(source, "record-main", "npc")
        candidate["connections"] = [{
            "connection_id": "connection_one",
            "target_record_id": "record-gate",
            "relationship": "connected-to",
            "state": "current",
            "context": "Watches the gate.",
        }]
        candidate["content_digest"] = document_digest(candidate)

        result = mutate_document(source, candidate)

        paragraph = "Authored paragraph stays intact.\nIts second line remains together."
        marker = "<!-- drydock:connection-id=connection_one -->"
        self.assertIn(paragraph, result)
        self.assertLess(result.index(paragraph), result.index(marker))
        self.assertEqual(candidate["connections"], parse_document(result, "record-main", "npc")["connections"])

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

    def test_section_reordering_is_rejected(self):
        source = """---
id: record-main
type: npc
name: Keeper
status: draft
visibility: warden
---

## First
First body.

## Second
Second body.
"""
        candidate = parse_document(source, "record-main", "npc")
        candidate["sections"] = list(reversed(candidate["sections"]))
        candidate["displayed_name"] = "Updated Keeper"
        candidate["content_digest"] = document_digest(candidate)

        with self.assertRaisesRegex(ValueError, "editor_section_reordering_not_allowed"):
            mutate_document(source, candidate)

    def test_field_reordering_is_rejected(self):
        source = """---
id: record-main
type: npc
name: Keeper
status: draft
visibility: warden
alpha: first
beta: second
---

## Summary
Keep this record.
"""
        candidate = parse_document(source, "record-main", "npc")
        candidate["fields"] = list(reversed(candidate["fields"]))
        candidate["displayed_name"] = "Updated Keeper"
        candidate["content_digest"] = document_digest(candidate)

        with self.assertRaisesRegex(ValueError, "editor_field_reordering_not_allowed"):
            mutate_document(source, candidate)

    def test_connection_reordering_is_rejected(self):
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

<!-- drydock:connection-id=connection_alpha -->
- `connected-to` -> [[record-gate]] (`current`) — Watches the gate.
<!-- drydock:connection-id=connection_beta -->
- `connected-to` -> [[record-port]] (`current`) — Watches the port.
"""
        candidate = parse_document(source, "record-main", "npc")
        candidate["connections"] = list(reversed(candidate["connections"]))
        candidate["displayed_name"] = "Updated Keeper"
        candidate["content_digest"] = document_digest(candidate)

        with self.assertRaisesRegex(ValueError, "editor_connection_reordering_not_allowed"):
            mutate_document(source, candidate)

    def test_section_reordering_with_new_section_is_rejected(self):
        source = """---
id: record-main
type: npc
name: Keeper
status: draft
visibility: warden
---

## First
First body.

## Second
Second body.
"""
        candidate = parse_document(source, "record-main", "npc")
        candidate["sections"] = [
            candidate["sections"][1],
            {"section_id": "inserted", "body": "Inserted body."},
            candidate["sections"][0],
        ]
        candidate["content_digest"] = document_digest(candidate)

        with self.assertRaisesRegex(ValueError, "editor_section_reordering_not_allowed"):
            mutate_document(source, candidate)

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

    def test_serialized_sections_use_bound_authored_labels(self):
        source = """---
id: record-main
type: npc
name: Keeper
status: draft
visibility: warden
---

## Current state
Keep this record.
"""
        candidate = parse_document(source, "record-main", "npc")

        serialized = serialize_document(candidate, {"current-state": "Current state"})

        self.assertIn("## Current state\n", serialized)
        self.assertNotIn("## current-state\n", serialized)

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
