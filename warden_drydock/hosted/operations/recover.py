from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import subprocess

from warden_drydock.hosted.revisions.store import FileSnapshotStore


_ID = re.compile(r"(?m)^id:\s*[\"']?([a-z][a-z0-9-]*)[\"']?\s*$")


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _query(database_url: str, sql: str) -> list[tuple[str, ...]]:
    result = subprocess.run(
        ["psql", database_url, "-X", "-At", "-F", "\t", "-v", "ON_ERROR_STOP=1", "-c", sql],
        check=True, capture_output=True, text=True,
    )
    return [tuple(line.split("\t")) for line in result.stdout.splitlines() if line]


def recover(database_url: str, snapshot_root: pathlib.Path) -> None:
    store = FileSnapshotStore(snapshot_root)
    inventory = store.inventory()
    by_revision = {(item.campaign_id, item.revision_id): item for item in inventory}
    intents = _query(
        database_url,
        "SELECT campaign_id,revision_id,intent_token,status,COALESCE(parent_revision,''),ordinal::text,tree_digest,change_digest FROM hosted_publication_intent ORDER BY campaign_id,ordinal",
    )
    if any(row[3] == "pending" for row in intents):
        raise RuntimeError("pending_publication_intent_requires_operator_recovery")
    finalized = {
        (row[0], row[1], row[2], row[4] or None, int(row[5]), row[6], row[7])
        for row in intents if row[3] == "finalized"
    }
    stored = {
        (item.campaign_id, item.revision_id, item.publication_intent_token,
         item.parent_revision, item.ordinal, item.tree_digest, item.change_digest)
        for item in inventory
    }
    if stored != finalized:
        raise RuntimeError("snapshot_inventory_and_finalized_intents_differ")
    campaigns: dict[str, list[object]] = {}
    for manifest in inventory:
        campaigns.setdefault(manifest.campaign_id, []).append(manifest)
    for manifests in campaigns.values():
        ordered = sorted(manifests, key=lambda item: (item.ordinal, item.revision_id))
        parent = None
        for ordinal, manifest in enumerate(ordered, 1):
            if manifest.ordinal != ordinal or manifest.parent_revision != parent:
                raise RuntimeError("snapshot_lineage_is_not_linear")
            parent = manifest.revision_id
    heads = _query(database_url, "SELECT campaign_id,revision_id FROM hosted_campaign_head ORDER BY campaign_id")
    statements = [
        "BEGIN",
        "SELECT pg_advisory_xact_lock(8231649237462)",
        "DELETE FROM hosted_projection_shadow_record",
        "DELETE FROM hosted_projection_shadow_checkpoint",
        "DELETE FROM hosted_projection_record",
        "DELETE FROM hosted_projection_checkpoint",
    ]
    for campaign_id, revision_id in heads:
        manifest = by_revision.get((campaign_id, revision_id))
        binding = None if manifest is None else (
            campaign_id, revision_id, manifest.publication_intent_token,
            manifest.parent_revision, manifest.ordinal, manifest.tree_digest,
            manifest.change_digest,
        )
        if manifest is None or binding not in finalized:
            raise RuntimeError("head_snapshot_or_intent_mismatch")
        statements.append("SELECT pg_advisory_xact_lock(hashtextextended(" + _literal(campaign_id) + ",0))")
        statements.append(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM hosted_campaign_head WHERE campaign_id="
            + _literal(campaign_id) + " AND revision_id=" + _literal(revision_id)
            + ") THEN RAISE EXCEPTION 'campaign head changed during recovery'; END IF; END $$"
        )
        verified = store.verify(manifest.tree_digest, campaign_id, revision_id)
        tree = store.snapshots / verified.tree_digest / campaign_id / revision_id / "tree"
        records: list[tuple[str, str, str]] = []
        for file_hash in verified.files:
            if file_hash.relative_path.endswith(".md"):
                match = _ID.search((tree / file_hash.relative_path).read_text(encoding="utf-8"))
                if match:
                    records.append((match.group(1), file_hash.relative_path, file_hash.sha256))
        records.sort()
        digest = hashlib.sha256(
            json.dumps(records, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        ).hexdigest()
        for record_id, relative_path, body_digest in records:
            values = ",".join(map(_literal, (campaign_id, revision_id, record_id, relative_path, body_digest)))
            statements.append("INSERT INTO hosted_projection_shadow_record(campaign_id,revision_id,record_id,relative_path,body_digest) VALUES(" + values + ")")
        checkpoint = ",".join(
            (_literal(campaign_id), _literal(revision_id), "1", str(len(records)), _literal(digest))
        )
        statements.append("INSERT INTO hosted_projection_shadow_checkpoint(campaign_id,revision_id,projection_version,record_count,projection_digest) VALUES(" + checkpoint + ")")
        statements.append("INSERT INTO hosted_projection_record SELECT * FROM hosted_projection_shadow_record WHERE campaign_id=" + _literal(campaign_id))
        statements.append("INSERT INTO hosted_projection_checkpoint SELECT * FROM hosted_projection_shadow_checkpoint WHERE campaign_id=" + _literal(campaign_id))
    statements.extend(
        (
            "UPDATE hosted_runtime_state SET maintenance_mode=false,reconciliation_complete=true,schema_compatibility=1,updated_at=now() WHERE singleton",
            "COMMIT",
        )
    )
    subprocess.run(
        ["psql", database_url, "-X", "-v", "ON_ERROR_STOP=1", "-f", "-"],
        input=";\n".join(statements) + ";\n", text=True, check=True,
    )


def main() -> None:
    recover(os.environ["DATABASE_URL"], pathlib.Path(os.environ["DRYDOCK_SNAPSHOTS"]))


if __name__ == "__main__":
    main()
