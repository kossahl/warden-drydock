from __future__ import annotations

from warden_drydock.hosted.revisions.models import ProjectionBundle


class InMemoryProjectionRepository:
    def __init__(self) -> None:
        self.active: dict[str, ProjectionBundle] = {}
        self.shadow: dict[str, ProjectionBundle] = {}
        self.operational: dict[str, object] = {}
        self.active_checkpoint: dict[str, ProjectionBundle] = {}
        self.shadow_checkpoint: dict[str, ProjectionBundle] = {}

    def stage(self, bundle: ProjectionBundle) -> None:
        self.shadow[bundle.campaign_id] = bundle
        self.shadow_checkpoint[bundle.campaign_id] = bundle

    def swap(
        self,
        campaign_id: str,
        expected_digest: str,
        expected_revision: str,
        workflow_repository,
    ) -> None:
        bundle = self.shadow[campaign_id]
        if bundle.projection_digest != expected_digest:
            raise ValueError("shadow projection digest mismatch")
        if (
            bundle.revision_id != expected_revision
            or workflow_repository.head(campaign_id) != expected_revision
        ):
            raise ValueError("campaign head changed before projection swap")
        self.active[campaign_id] = bundle
        self.active_checkpoint[campaign_id] = bundle
        del self.shadow[campaign_id]
        del self.shadow_checkpoint[campaign_id]


class PostgresProjectionRepository:
    def __init__(self, connect) -> None:
        self._connect = connect

    def stage(self, bundle: ProjectionBundle) -> None:
        connection = self._connect()
        try:
            with connection, connection.cursor() as cursor:
                cursor.execute("DELETE FROM hosted_projection_shadow_record WHERE campaign_id=%s", (bundle.campaign_id,))
                for record_id, relative_path, body_digest in bundle.records:
                    cursor.execute("INSERT INTO hosted_projection_shadow_record(campaign_id,revision_id,record_id,relative_path,body_digest) VALUES(%s,%s,%s,%s,%s)", (bundle.campaign_id, bundle.revision_id, record_id, relative_path, body_digest))
                cursor.execute("INSERT INTO hosted_projection_shadow_checkpoint(campaign_id,revision_id,projection_version,record_count,projection_digest) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(campaign_id) DO UPDATE SET revision_id=EXCLUDED.revision_id,projection_version=EXCLUDED.projection_version,record_count=EXCLUDED.record_count,projection_digest=EXCLUDED.projection_digest", (bundle.campaign_id, bundle.revision_id, bundle.projection_version, bundle.record_count, bundle.projection_digest))
        finally:
            connection.close()

    def swap(
        self,
        campaign_id: str,
        expected_digest: str,
        expected_revision: str,
        workflow_repository,
    ) -> None:
        connection = self._connect()
        try:
            with connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (campaign_id,),
                )
                cursor.execute(
                    "SELECT revision_id FROM hosted_campaign_head WHERE campaign_id=%s FOR UPDATE",
                    (campaign_id,),
                )
                head = cursor.fetchone()
                if head is None or head[0] != expected_revision:
                    raise ValueError(
                        "campaign head changed before projection swap"
                    )
                cursor.execute("SELECT revision_id,projection_version,record_count,projection_digest FROM hosted_projection_shadow_checkpoint WHERE campaign_id=%s FOR UPDATE", (campaign_id,))
                row = cursor.fetchone()
                if (
                    row is None
                    or row[0] != expected_revision
                    or row[3] != expected_digest
                ):
                    raise ValueError("shadow projection digest mismatch")
                cursor.execute("SELECT record_id,relative_path,body_digest FROM hosted_projection_shadow_record WHERE campaign_id=%s ORDER BY record_id,relative_path,body_digest", (campaign_id,))
                records = tuple(cursor.fetchall())
                import hashlib, json
                actual_digest = hashlib.sha256(
                    json.dumps(records, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
                ).hexdigest()
                if len(records) != row[2] or actual_digest != expected_digest:
                    raise ValueError("persisted shadow projection verification failed")
                cursor.execute("DELETE FROM hosted_projection_record WHERE campaign_id=%s", (campaign_id,))
                cursor.execute("INSERT INTO hosted_projection_record SELECT * FROM hosted_projection_shadow_record WHERE campaign_id=%s", (campaign_id,))
                cursor.execute("INSERT INTO hosted_projection_checkpoint(campaign_id,revision_id,projection_version,record_count,projection_digest) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(campaign_id) DO UPDATE SET revision_id=EXCLUDED.revision_id,projection_version=EXCLUDED.projection_version,record_count=EXCLUDED.record_count,projection_digest=EXCLUDED.projection_digest", (campaign_id, row[0], row[1], row[2], row[3]))
                cursor.execute("DELETE FROM hosted_projection_shadow_record WHERE campaign_id=%s", (campaign_id,))
                cursor.execute("DELETE FROM hosted_projection_shadow_checkpoint WHERE campaign_id=%s", (campaign_id,))
        finally:
            connection.close()
