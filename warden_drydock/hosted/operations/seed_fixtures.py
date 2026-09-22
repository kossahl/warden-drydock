from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path

from warden_drydock import __version__
from warden_drydock.hosted.projections import (
    AtlasProjectionRebuilder,
    PostgresAtlasProjectionRepository,
)
from warden_drydock.hosted.projections.atlas_models import canonical_digest
from warden_drydock.hosted.revisions import (
    FileSnapshotStore,
    IntentStatus,
    PostgresWorkflowRepository,
    PublicationIntent,
    PublicationKind,
    RevisionService,
    canonicalize_tree,
)


FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "data" / "fixtures"
HOSTED_ADAPTER_VERSION = "1.0.0"


@dataclass(frozen=True)
class CampaignFixture:
    campaign_id: str
    campaign_name: str
    adapter_id: str
    relative_path: str


EREBOS_STATION_DEMO = CampaignFixture(
    campaign_id="campaign_mt8j9g9s_1",
    campaign_name="Erebos Station Demo",
    adapter_id="mothership",
    relative_path="erebos-station-demo",
)


def _seed_identity(
    fixture: CampaignFixture,
    tree_digest: str,
    store: FileSnapshotStore,
    workflow,
) -> tuple[str, str, str]:
    """Choose a deterministic identity that is not poisoned by a quarantine."""
    attempt = 0
    while True:
        suffix = (
            tree_digest[:20]
            if attempt == 0
            else canonical_digest(
                {"attempt": attempt, "tree_digest": tree_digest}
            )[:20]
        )
        revision_id = f"revision_fixture_{suffix}"
        intent_id = f"intent_fixture_{fixture.campaign_id}_{suffix}"
        intent_token = f"token_fixture_{suffix}"
        matches = workflow.matching_intents(intent_token)
        quarantined = (
            store.quarantine
            / tree_digest
            / fixture.campaign_id
            / revision_id
        ).exists()
        if not matches and not quarantined:
            return revision_id, intent_id, intent_token
        if matches and not all(
            item.status is IntentStatus.QUARANTINED for item in matches
        ):
            raise RuntimeError("fixture seed identity is already in use")
        attempt += 1


def seed_campaign(
    fixture: CampaignFixture,
    source: Path,
    snapshot_root: Path,
    workflow,
    atlas,
    *,
    validation_contract_digest: str,
):
    """Publish one fixture without replacing an existing campaign head."""
    if workflow.head(fixture.campaign_id) is not None:
        return None

    source = source.resolve()
    metadata = source / ".drydock.json"
    if not source.is_dir() or not metadata.is_file():
        raise RuntimeError(f"fixture tree is missing: {source}")
    declared = json.loads(metadata.read_text(encoding="utf-8"))
    if (
        declared.get("campaign_name") != fixture.campaign_name
        or declared.get("adapter") != fixture.adapter_id
    ):
        raise RuntimeError("fixture metadata does not match its seed definition")

    store = FileSnapshotStore(snapshot_root)
    _, tree_digest = canonicalize_tree(source)
    revision_id, intent_id, intent_token = _seed_identity(
        fixture, tree_digest, store, workflow
    )
    change_digest = canonical_digest(
        {
            "adapter_id": fixture.adapter_id,
            "campaign_id": fixture.campaign_id,
            "campaign_name": fixture.campaign_name,
            "fixture": fixture.relative_path,
            "tree_digest": tree_digest,
        }
    )
    intent = PublicationIntent(
        intent_id,
        intent_token,
        PublicationKind.CREATION,
        fixture.campaign_id,
        revision_id,
        None,
        1,
        tree_digest,
        change_digest,
    )

    revisions = RevisionService(store, workflow)
    rebuilder = AtlasProjectionRebuilder(store, atlas, workflow)
    return revisions.publish(
        source,
        intent,
        framework_version=__version__,
        adapter_version=HOSTED_ADAPTER_VERSION,
        validation_contract_digest=validation_contract_digest,
        before_finalize=rebuilder.rebuild_pending,
        rollback=lambda manifest: atlas.delete(
            manifest.campaign_id, manifest.revision_id
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the local MVP campaign fixture")
    parser.parse_args()

    import psycopg
    from warden_drydock.hosted.http.application import SliceApplication

    database_url = os.environ["DATABASE_URL"]
    snapshot_root = Path(os.environ["DRYDOCK_SNAPSHOTS"])
    connect = lambda: psycopg.connect(database_url)
    workflow = PostgresWorkflowRepository(connect)
    atlas = PostgresAtlasProjectionRepository(connect)
    fixture = EREBOS_STATION_DEMO
    source = FIXTURE_ROOT / fixture.relative_path
    manifest = seed_campaign(
        fixture,
        source,
        snapshot_root,
        workflow,
        atlas,
        validation_contract_digest=SliceApplication.validation_contract_digest,
    )
    if manifest is None:
        print(f"Fixture already present; skipped {fixture.campaign_id}")
        return
    print(
        f"Seeded {fixture.campaign_name} as {fixture.campaign_id} "
        f"at {manifest.revision_id} ({manifest.tree_digest})"
    )


if __name__ == "__main__":
    main()
