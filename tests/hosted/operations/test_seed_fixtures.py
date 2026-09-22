from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from warden_drydock.hosted.operations.seed_fixtures import (
    CampaignFixture,
    EREBOS_STATION_DEMO,
    FIXTURE_ROOT,
    seed_campaign,
)
from warden_drydock.hosted.projections import InMemoryAtlasProjectionRepository
from warden_drydock.hosted.revisions import (
    FileSnapshotStore,
    InMemoryWorkflowRepository,
)


class FixtureSeedTests(unittest.TestCase):
    def test_repository_fixture_is_the_selected_erebos_campaign(self) -> None:
        source = FIXTURE_ROOT / EREBOS_STATION_DEMO.relative_path
        declared = json.loads((source / ".drydock.json").read_text(encoding="utf-8"))

        self.assertTrue(source.is_dir())
        self.assertEqual("Erebos Station Demo", declared["campaign_name"])
        self.assertEqual("mothership", declared["adapter"])

    def source(self, root: Path) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        (root / ".drydock.json").write_text(
            json.dumps(
                {
                    "framework": "warden-drydock",
                    "framework_version": "0.2.0",
                    "adapter": "mothership",
                    "adapter_version": "0.2.0",
                    "ownership_model": 1,
                    "campaign_name": "Fixture Campaign",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (root / "01-campaign").mkdir()
        (root / "01-campaign" / "campaign-overview.md").write_text(
            "---\n"
            "id: campaign-main\n"
            "type: campaign\n"
            "status: draft\n"
            "ownership: campaign\n"
            "name: Fixture Campaign\n"
            "system: mothership\n"
            "---\n\n"
            "# Fixture Campaign\n\n"
            "## Summary\n\n"
            "A deterministic fixture.\n",
            encoding="utf-8",
        )
        return root

    def test_seed_publishes_projection_and_repeated_seed_is_a_noop(self) -> None:
        fixture = CampaignFixture(
            "campaign_fixture",
            "Fixture Campaign",
            "mothership",
            "test-fixture",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.source(root / "source")
            snapshots = root / "snapshots"
            workflow = InMemoryWorkflowRepository()
            atlas = InMemoryAtlasProjectionRepository()

            manifest = seed_campaign(
                fixture,
                source,
                snapshots,
                workflow,
                atlas,
                validation_contract_digest="f" * 64,
            )
            self.assertIsNotNone(manifest)
            self.assertEqual(manifest.revision_id, workflow.head(fixture.campaign_id))
            self.assertEqual(1, len(atlas.list(fixture.campaign_id)))

            repeated = seed_campaign(
                fixture,
                source,
                snapshots,
                workflow,
                atlas,
                validation_contract_digest="f" * 64,
            )
            self.assertIsNone(repeated)
            self.assertEqual(1, len(FileSnapshotStore(snapshots).inventory()))

    def test_existing_campaign_is_not_overwritten(self) -> None:
        fixture = CampaignFixture(
            "campaign_fixture",
            "Fixture Campaign",
            "mothership",
            "test-fixture",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow = InMemoryWorkflowRepository()
            workflow.heads[fixture.campaign_id] = ("revision_existing", 1)
            atlas = InMemoryAtlasProjectionRepository()

            result = seed_campaign(
                fixture,
                self.source(root / "source"),
                root / "snapshots",
                workflow,
                atlas,
                validation_contract_digest="f" * 64,
            )
            self.assertIsNone(result)
            self.assertEqual((), FileSnapshotStore(root / "snapshots").inventory())


if __name__ == "__main__":
    unittest.main()
