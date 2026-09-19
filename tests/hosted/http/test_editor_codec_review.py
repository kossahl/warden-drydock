from __future__ import annotations

import re
import unittest

from warden_drydock.hosted.http.editor import (
    document_digest,
    mutate_document,
    parse_document,
    serialize_document,
)


def _document(**overrides):
    value = {
        "record_id": "record-one",
        "record_type": "npc",
        "displayed_name": "The One",
        "ownership": "campaign",
        "status": "draft",
        "authority": "preparation",
        "visibility": {"audience": "warden", "warden_only": True},
        "fields": [],
        "sections": [{"section_id": "summary", "body": "A record."}],
        "connections": [],
        "content_digest": "",
    }
    value.update(overrides)
    value["content_digest"] = document_digest(value)
    return value


class EditorCodecReviewTests(unittest.TestCase):
    def test_metadata_field_ids_are_reserved(self):
        for field_id in ("id", "type", "name", "status", "ownership", "visibility", "warden_only"):
            with self.subTest(field_id=field_id):
                value = _document(fields=[{"field_id": field_id, "value": "spoof"}])
                with self.assertRaisesRegex(ValueError, "unsafe_identifier"):
                    serialize_document(value)

    def test_record_documents_are_campaign_owned(self):
        source = (
            "---\n"
            "id: record-one\n"
            "type: npc\n"
            "status: draft\n"
            "ownership: shared\n"
            "visibility: warden\n"
            "warden_only: true\n"
            "---\n\n"
            "# The One\n\n"
            "## Summary\n\n"
            "A record.\n"
        )
        parsed = parse_document(source, "record-one", "npc")
        self.assertEqual("campaign", parsed["ownership"])
        self.assertNotIn("ownership", {field["field_id"] for field in parsed["fields"]})
        self.assertIn("ownership: campaign\n", serialize_document(parsed))
        candidate = dict(parsed, sections=[{"section_id": "summary", "body": "Changed."}])
        candidate["content_digest"] = document_digest(candidate)
        self.assertIn("ownership: campaign\n", mutate_document(source, candidate))

    def test_missing_and_unknown_source_statuses_are_read_only(self):
        for raw_status, expected in ((None, {"classification": "missing", "value": None}), ("future", {"classification": "unknown", "value": "future"})):
            with self.subTest(raw_status=raw_status):
                status_line = "" if raw_status is None else f"status: {raw_status}\n"
                source = (
                    "---\n"
                    "id: record-one\n"
                    "type: npc\n"
                    f"{status_line}"
                    "ownership: campaign\n"
                    "visibility: warden\n"
                    "warden_only: true\n"
                    "---\n\n"
                    "# The One\n\n"
                    "## Summary\n\n"
                    "A record.\n"
                )
                parsed = parse_document(source, "record-one", "npc")
                self.assertEqual(expected, parsed["status"])
                self.assertEqual("preparation", parsed["authority"])
                with self.assertRaisesRegex(ValueError, "invalid_status"):
                    serialize_document(parsed)

                candidate = dict(parsed, status="draft")
                candidate["content_digest"] = document_digest(candidate)
                self.assertIn("status: draft\n", mutate_document(source, candidate))

    def test_mutation_parses_the_source_with_source_identity(self):
        source = (
            "---\n"
            "id: source-record\n"
            "type: source-type\n"
            "name: The One\n"
            "ownership: campaign\n"
            "status: draft\n"
            "visibility: warden\n"
            "warden_only: true\n"
            "---\n\n"
            "# The One\n\n"
            "## Summary\n\n"
            "A record.\n"
        )
        candidate = _document(record_id="candidate-record", record_type="candidate-type")
        result = mutate_document(source, candidate)
        self.assertIn("id: candidate-record\n", result)
        self.assertIn("type: candidate-type\n", result)

    def test_new_serialization_has_one_named_top_level_h1(self):
        serialized = serialize_document(_document())
        headings = re.findall(r"^# (?!#)(.+)$", serialized, re.MULTILINE)
        self.assertEqual(["The One"], headings)


if __name__ == "__main__":
    unittest.main()
