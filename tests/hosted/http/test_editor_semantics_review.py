from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest

from warden_drydock.hosted.http.editor_semantics import (
    EditorSemanticError,
    _property_changes,
    validate_editor_semantics,
)


ROOT = Path(__file__).resolve().parents[3]
EXAMPLES = json.loads(
    (ROOT / "docs/contracts/hosted/http/editor/v1/examples.json").read_text(
        encoding="utf-8"
    )
)


def _example(name: str) -> dict:
    return next(item["payload"] for item in EXAMPLES["examples"] if item["name"] == name)


class EditorSemanticsReviewTests(unittest.TestCase):
    def test_property_changes_preserve_presence(self) -> None:
        before = {
            "displayed_name": "Record",
            "status": "draft",
            "authority": "preparation",
            "visibility": {"audience": "warden", "warden_only": True},
            "fields": [{"field_id": "nullable", "value": None}],
            "sections": [{"section_id": "summary", "body": "old"}],
        }
        after = {
            "displayed_name": "Record",
            "status": "draft",
            "authority": "preparation",
            "visibility": {"audience": "warden", "warden_only": True},
            "fields": [{"field_id": "added", "value": None}],
            "sections": [{"section_id": "summary", "body": "new"}],
        }

        changes = _property_changes(before, after)

        self.assertEqual(
            changes,
            [
                {
                    "property": "fields.nullable",
                    "before": None,
                    "after": None,
                    "before_present": True,
                    "after_present": False,
                },
                {
                    "property": "fields.added",
                    "before": None,
                    "after": None,
                    "before_present": False,
                    "after_present": True,
                },
                {
                    "property": "sections.summary",
                    "before": "old",
                    "after": "new",
                    "before_present": True,
                    "after_present": True,
                },
            ],
        )

    def test_record_digest_mismatch_is_a_digest_conflict(self) -> None:
        record = deepcopy(_example("head_record_view")["record"])
        record["content_digest"] = "0" * 64

        with self.assertRaises(EditorSemanticError) as raised:
            validate_editor_semantics({**_example("head_record_view"), "record": record})

        self.assertEqual(raised.exception.category, "idempotency_digest_conflict")

    def test_removal_impact_rejects_forged_reference_target(self) -> None:
        payload = deepcopy(_example("removal_impact"))
        payload["incoming_references"][0]["target_record_id"] = "record-other"

        with self.assertRaises(EditorSemanticError) as raised:
            validate_editor_semantics(payload)

        self.assertEqual(raised.exception.category, "unsafe_binding")

    def test_source_snapshot_is_bound_to_structured_record(self) -> None:
        payload = deepcopy(_example("editor_proposal_view"))
        payload["diff"]["source_changes"][0]["before_source"] = payload["diff"]["source_changes"][0]["before_source"].replace(
            "A quiet station.", "A forged station."
        )

        with self.assertRaises(EditorSemanticError) as raised:
            validate_editor_semantics(payload)

        self.assertEqual(raised.exception.category, "mutation_consistency")
