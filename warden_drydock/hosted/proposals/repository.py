from __future__ import annotations

from contextlib import contextmanager
import json

from warden_drydock.hosted.engine.models import ChangeKind, ExactTextChange
from warden_drydock.hosted.revisions.models import SnapshotManifest

from .service import ProposalStatus, ProposalVersion


def _encode_changes(changes):
    return [{"change_id": c.change_id, "subject_id": c.subject_id,
             "expected_content_digest": c.expected_content_digest,
             "replacement": c.replacement, "change_kind": c.change_kind.value,
             "record_type": c.record_type} for c in changes]


def _decode_changes(value):
    if isinstance(value, str):
        value = json.loads(value)
    return tuple(ExactTextChange(item["change_id"], item["subject_id"],
        item["expected_content_digest"], item["replacement"],
        ChangeKind(item["change_kind"]), item["record_type"]) for item in value)


class PostgresProposalRepository:
    """Transactional PostgreSQL authority for proposal versions and audit."""

    def __init__(self, connect):
        self._connect = connect

    @contextmanager
    def _transaction(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _item(row):
        if row is None:
            return None
        return ProposalVersion(row[0], row[1], row[2], row[3],
            _decode_changes(row[4]), row[5], row[6], ProposalStatus(row[7]),
            row[8], row[9], row[10], row[11], row[12])

    @staticmethod
    def _select(cursor, proposal_id, version, *, lock=False):
        cursor.execute("SELECT proposal_id,version,campaign_id,base_revision,changes,diff_digest,payload_digest,status,generation_id,source_revision,source_set_digest,terminal_draft_digest,published_revision_id FROM hosted_proposal_version WHERE proposal_id=%s AND version=%s" + (" FOR UPDATE" if lock else ""), (proposal_id, version))
        return PostgresProposalRepository._item(cursor.fetchone())

    @staticmethod
    def _audit(cursor, item, event, *, intent_id=None, revision_id=None, result_digest=None):
        cursor.execute("INSERT INTO hosted_proposal_audit(proposal_id,version,status,diff_digest,payload_digest,event,publication_intent_token,published_revision_id,result_digest) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (item.proposal_id, item.version, item.status.value, item.diff_digest,
             item.payload_digest, event, intent_id, revision_id, result_digest))

    def next_version(self, proposal_id):
        with self._transaction() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT COALESCE(MAX(version),0)+1 FROM hosted_proposal_version WHERE proposal_id=%s", (proposal_id,))
            return cursor.fetchone()[0]

    def add(self, item):
        with self._transaction() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 2))", (item.proposal_id,))
            cursor.execute("SELECT COALESCE(MAX(version),0)+1 FROM hosted_proposal_version WHERE proposal_id=%s", (item.proposal_id,))
            if cursor.fetchone()[0] != item.version:
                raise ValueError("proposal_version_conflict")
            cursor.execute("INSERT INTO hosted_proposal_version(proposal_id,version,campaign_id,base_revision,changes,diff_digest,payload_digest,status,generation_id,source_revision,source_set_digest,terminal_draft_digest) VALUES(%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s)",
                (item.proposal_id, item.version, item.campaign_id, item.base_revision,
                 json.dumps(_encode_changes(item.changes)), item.diff_digest,
                 item.payload_digest, item.status.value, item.generation_id,
                 item.source_revision, item.source_set_digest,
                 item.terminal_draft_digest))
            self._audit(cursor, item, "created")

    def get(self, proposal_id, version):
        with self._transaction() as connection, connection.cursor() as cursor:
            return self._select(cursor, proposal_id, version)

    def versions(self, proposal_id):
        with self._transaction() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT proposal_id,version,campaign_id,base_revision,changes,diff_digest,payload_digest,status,generation_id,source_revision,source_set_digest,terminal_draft_digest,published_revision_id FROM hosted_proposal_version WHERE proposal_id=%s ORDER BY version", (proposal_id,))
            return tuple(self._item(row) for row in cursor.fetchall())

    def workflow_counts(self, campaign_id, revision_id):
        values = {name: 0 for name in ("draft", "rejected", "conflict", "published", "quarantined")}
        with self._transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT status,count(*) FROM hosted_proposal_version "
                "WHERE campaign_id=%s AND base_revision=%s "
                "AND status IN ('draft','rejected','conflict','published','quarantined') "
                "GROUP BY status",
                (campaign_id, revision_id),
            )
            for status, count in cursor.fetchall():
                values[status] = count
        return values

    def find_by_published_revision(self, campaign_id, revision_id):
        with self._transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT proposal_id,version,campaign_id,base_revision,changes,diff_digest,payload_digest,status,generation_id,source_revision,source_set_digest,terminal_draft_digest,published_revision_id "
                "FROM hosted_proposal_version WHERE campaign_id=%s AND published_revision_id=%s",
                (campaign_id, revision_id),
            )
            rows = cursor.fetchall()
        if len(rows) > 1:
            raise ValueError("proposal_publication_binding_conflict")
        return self._item(rows[0]) if rows else None

    def _transition(self, item, expected, status, event):
        with self._transaction() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE hosted_proposal_version SET status=%s WHERE proposal_id=%s AND version=%s AND status=%s RETURNING proposal_id,version,campaign_id,base_revision,changes,diff_digest,payload_digest,status,generation_id,source_revision,source_set_digest,terminal_draft_digest,published_revision_id",
                (status.value, item.proposal_id, item.version, expected.value))
            updated = self._item(cursor.fetchone())
            if updated is not None:
                self._audit(cursor, updated, event)
            return updated

    def claim(self, item):
        return self._transition(item, ProposalStatus.DRAFT, ProposalStatus.APPROVING, "approval_claimed")

    def reject(self, item):
        current = self.get(item.proposal_id, item.version)
        if current is not None and current.status is ProposalStatus.REJECTED:
            return current
        return self._transition(item, ProposalStatus.DRAFT, ProposalStatus.REJECTED, "rejected")

    def replace_status(self, item, status):
        updated = self._transition(item, item.status, status, status.value)
        if updated is None:
            current = self.get(item.proposal_id, item.version)
            if current is not None and current.status is status:
                return current
            raise ValueError("proposal_status_conflict")
        return updated

    def correct(self, item, changes, base_revision, *, diff_digest=None, payload_digest=None):
        from .service import _payload_digest, _diff_digest
        diff_digest = diff_digest or _diff_digest
        payload_digest = payload_digest or _payload_digest
        if not changes or len({change.change_id for change in changes}) != len(changes):
            raise ValueError("proposal changes must be non-empty and uniquely identified")
        with self._transaction() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 2))", (item.proposal_id,))
            current = self._select(cursor, item.proposal_id, item.version, lock=True)
            if current is None or current.status not in (ProposalStatus.DRAFT, ProposalStatus.CONFLICT):
                return None
            cursor.execute("SELECT COALESCE(MAX(version),0)+1 FROM hosted_proposal_version WHERE proposal_id=%s", (item.proposal_id,))
            version = cursor.fetchone()[0]
            corrected = ProposalVersion(
                item.proposal_id, version, item.campaign_id,
                base_revision or current.base_revision, changes,
                diff_digest(changes), payload_digest(changes),
                generation_id=current.generation_id,
                source_revision=current.source_revision,
                source_set_digest=current.source_set_digest,
                terminal_draft_digest=current.terminal_draft_digest,
            )
            cursor.execute("UPDATE hosted_proposal_version SET status='rejected' WHERE proposal_id=%s AND version=%s AND status=%s", (item.proposal_id, item.version, current.status.value))
            if cursor.rowcount != 1:
                raise ValueError("proposal_status_conflict")
            cursor.execute("INSERT INTO hosted_proposal_version(proposal_id,version,campaign_id,base_revision,changes,diff_digest,payload_digest,status,generation_id,source_revision,source_set_digest,terminal_draft_digest) VALUES(%s,%s,%s,%s,%s::jsonb,%s,%s,'draft',%s,%s,%s,%s)", (corrected.proposal_id, corrected.version, corrected.campaign_id, corrected.base_revision, json.dumps(_encode_changes(changes)), corrected.diff_digest, corrected.payload_digest, corrected.generation_id, corrected.source_revision, corrected.source_set_digest, corrected.terminal_draft_digest))
            self._audit(cursor, ProposalVersion(**{**current.__dict__, "status": ProposalStatus.REJECTED}), "corrected")
            self._audit(cursor, corrected, "created")
            return corrected

    @staticmethod
    def _link(result):
        if not isinstance(result, SnapshotManifest):
            raise ValueError("publication linkage requires a snapshot manifest")
        intent_id = getattr(result, "publication_intent_token", None)
        revision_id = getattr(result, "revision_id", None)
        result_digest = getattr(result, "tree_digest", None)
        return intent_id, revision_id, result_digest

    def finalize(self, item, status, result=None):
        if result is None and status is ProposalStatus.APPROVED:
            intent_id, revision_id, result_digest = (None, None, None)
        else:
            intent_id, revision_id, result_digest = self._link(result)
        with self._transaction() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE hosted_proposal_version SET status=%s,publication_intent_token=COALESCE(publication_intent_token,%s),published_revision_id=COALESCE(published_revision_id,%s),result_digest=COALESCE(result_digest,%s) WHERE proposal_id=%s AND version=%s AND status IN ('approving','approved','quarantined') RETURNING proposal_id,version,campaign_id,base_revision,changes,diff_digest,payload_digest,status,generation_id,source_revision,source_set_digest,terminal_draft_digest,published_revision_id",
                (status.value, intent_id, revision_id, result_digest, item.proposal_id, item.version))
            updated = self._item(cursor.fetchone())
            if updated is None:
                current = self._select(cursor, item.proposal_id, item.version)
                if current is not None and current.status is status:
                    cursor.execute("SELECT publication_intent_token,published_revision_id,result_digest FROM hosted_proposal_version WHERE proposal_id=%s AND version=%s", (item.proposal_id, item.version))
                    stored = cursor.fetchone()
                    expected = (intent_id, revision_id, result_digest)
                    if any(value is not None and stored[index] != value for index, value in enumerate(expected)):
                        raise ValueError("proposal_publication_binding_conflict")
                    return current
                raise ValueError("proposal_publication_conflict")
            self._audit(cursor, updated, status.value, intent_id=intent_id,
                revision_id=revision_id, result_digest=result_digest)
            return updated

    def audit(self, proposal_id):
        with self._transaction() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT version,status,diff_digest,payload_digest,event,publication_intent_token,published_revision_id,result_digest FROM hosted_proposal_audit WHERE proposal_id=%s ORDER BY audit_id", (proposal_id,))
            return tuple(cursor.fetchall())
