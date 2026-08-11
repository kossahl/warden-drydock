from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from tests.hosted.contracts.test_contracts import contract_errors
from tests.hosted.engine.test_engine import EngineTestCase
from warden_drydock.hosted.engine import (
    ContractMappingError,
    RetrievalContractBinding,
    RetrievalSourceBinding,
    SourceAuthority,
    WorkspaceRequest,
    to_engine_staged_result_v1,
    to_retrieval_source_envelope_v1,
)


SCHEMA_ROOT = Path(__file__).resolve().parents[3] / "docs" / "contracts" / "hosted" / "schemas" / "v1"


class ContractV1MappingTests(EngineTestCase):
    @staticmethod
    def schema(name: str) -> dict:
        return json.loads((SCHEMA_ROOT / name).read_text(encoding="utf-8"))

    def test_engine_result_maps_to_exact_accepted_v1_shape(self) -> None:
        result = self.engine.index(WorkspaceRequest("command_index", self.handle))
        payload = to_engine_staged_result_v1(result)
        self.assertEqual(
            {
                "contract_name",
                "contract_version",
                "command_id",
                "command",
                "snapshot_handle",
                "staged_handle",
                "input_digest",
                "result_digest",
                "status",
                "findings",
            },
            set(payload),
        )
        self.assertNotIn("artifact_ids", payload)
        schema = self.schema("engine.schema.json")
        self.assertEqual([], list(Draft202012Validator(schema).iter_errors(payload)))
        self.assertEqual([], list(contract_errors(payload, schema)))

    def test_command_id_boundary_always_serializes_to_valid_engine_v1(self) -> None:
        maximum = "c" + "0" * 79
        success = self.engine.validate(WorkspaceRequest(maximum, self.handle))
        self.assertEqual(maximum, success.command_id)
        schema = self.schema("engine.schema.json")
        success_payload = to_engine_staged_result_v1(success)
        self.assertEqual([], list(contract_errors(success_payload, schema)))

        one_over = "c" + "0" * 80
        failure = self.engine.validate(WorkspaceRequest(one_over, self.handle))
        self.assertEqual("invalid_command", failure.command_id)
        failure_payload = to_engine_staged_result_v1(failure)
        self.assertEqual([], list(contract_errors(failure_payload, schema)))

        unknown_max_handle = self.handle.__class__("w" + "0" * 79)
        handle_failure = self.engine.validate(
            WorkspaceRequest("command_validate", unknown_max_handle)
        )
        self.assertEqual(
            [],
            list(
                contract_errors(
                    to_engine_staged_result_v1(handle_failure), schema
                )
            ),
        )

    def test_retrieval_maps_only_to_accepted_retrieval_contract(self) -> None:
        retrieval = self.show()
        with self.assertRaises(ContractMappingError):
            to_engine_staged_result_v1(retrieval.result)

        binding = RetrievalContractBinding(
            "campaign_alpha",
            "revision_0001",
            1,
            (
                RetrievalSourceBinding(
                    "campaign-main",
                    "entity_campaign_main",
                    SourceAuthority.PREPARATION,
                ),
            ),
        )
        first = to_retrieval_source_envelope_v1(retrieval, binding)
        second = to_retrieval_source_envelope_v1(retrieval, binding)
        self.assertEqual(first, second)
        self.assertEqual("retrieval_source_envelope", first["contract_name"])
        self.assertEqual(1, first["source_count"])
        self.assertEqual(1, first["excerpt_count"])
        schema = self.schema("retrieval.schema.json")
        self.assertEqual([], list(Draft202012Validator(schema).iter_errors(first)))
        self.assertEqual([], list(contract_errors(first, schema)))
        serialized = json.dumps(first, sort_keys=True)
        self.assertNotIn('"command": "retrieve"', serialized)
        self.assertNotIn('"stage": "retrieve"', serialized)

    def test_retrieval_binding_enforces_accepted_public_id_bounds(self) -> None:
        retrieval = self.show()
        maximum = "s" + "0" * 79
        binding = RetrievalContractBinding(
            "campaign_alpha",
            "revision_0001",
            1,
            (
                RetrievalSourceBinding(
                    "campaign-main", maximum, SourceAuthority.PREPARATION
                ),
            ),
        )
        payload = to_retrieval_source_envelope_v1(retrieval, binding)
        schema = self.schema("retrieval.schema.json")
        self.assertEqual([], list(contract_errors(payload, schema)))

        one_over = "s" + "0" * 80
        invalid = RetrievalContractBinding(
            "campaign_alpha",
            "revision_0001",
            1,
            (
                RetrievalSourceBinding(
                    "campaign-main", one_over, SourceAuthority.PREPARATION
                ),
            ),
        )
        with self.assertRaises(ContractMappingError):
            to_retrieval_source_envelope_v1(retrieval, invalid)


if __name__ == "__main__":
    import unittest

    unittest.main()
