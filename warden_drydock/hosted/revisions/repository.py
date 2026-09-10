from __future__ import annotations

from contextlib import contextmanager
import json
from typing import Iterator, Protocol

from .models import (
    IntentStatus,
    PublicationIntent,
    PublicationIntentError,
    PublicationKind,
    StaleHeadError,
)


class WorkflowRepository(Protocol):
    def add_intent(self, intent: PublicationIntent) -> None: ...
    def matching_intents(self, token: str) -> tuple[PublicationIntent, ...]: ...
    def finalize_head(self, intent: PublicationIntent) -> bool: ...
    def quarantine_intent(self, intent_id: str) -> None: ...
    def head(self, campaign_id: str) -> str | None: ...
    def publication_eligible(self, manifest) -> bool: ...


class InMemoryWorkflowRepository:
    """Transactional semantics model used by services and fault tests."""

    def __init__(self) -> None:
        self.intents: dict[str, PublicationIntent] = {}
        self.heads: dict[str, tuple[str, int]] = {}
        self.audit: list[tuple[str, str]] = []

    def add_intent(self, intent: PublicationIntent) -> None:
        existing = self.intents.get(intent.intent_id)
        if existing is not None and existing != intent:
            raise ValueError("intent identity conflict")
        token_owner = next(
            (
                item
                for item in self.intents.values()
                if item.intent_token == intent.intent_token
                and item.intent_id != intent.intent_id
            ),
            None,
        )
        if token_owner is not None:
            raise PublicationIntentError("publication intent token conflict")
        self.intents[intent.intent_id] = intent

    def matching_intents(self, token: str) -> tuple[PublicationIntent, ...]:
        return tuple(intent for intent in self.intents.values() if intent.intent_token == token)

    def finalize_head(self, intent: PublicationIntent) -> bool:
        stored = self.intents[intent.intent_id]
        if stored.status is IntentStatus.FINALIZED:
            return False
        current = self.heads.get(intent.campaign_id)
        expected = None if current is None else current[0]
        expected_ordinal = 1 if current is None else current[1] + 1
        if expected != intent.parent_revision or expected_ordinal != intent.ordinal:
            raise StaleHeadError("campaign head compare-and-swap failed")
        self.heads[intent.campaign_id] = (intent.revision_id, intent.ordinal)
        self.intents[intent.intent_id] = PublicationIntent(**{**intent.__dict__, "status": IntentStatus.FINALIZED})
        self.audit.append((intent.intent_id, "finalized"))
        return True

    def quarantine_intent(self, intent_id: str) -> None:
        intent = self.intents[intent_id]
        self.intents[intent_id] = PublicationIntent(**{**intent.__dict__, "status": IntentStatus.QUARANTINED})
        self.audit.append((intent_id, "quarantined"))

    def head(self, campaign_id: str) -> str | None:
        value = self.heads.get(campaign_id)
        return value[0] if value else None

    def publication_eligible(self, manifest) -> bool:
        matches = self.matching_intents(manifest.publication_intent_token)
        return (
            len(matches) == 1
            and matches[0].status is IntentStatus.FINALIZED
            and matches[0].campaign_id == manifest.campaign_id
            and matches[0].revision_id == manifest.revision_id
            and matches[0].parent_revision == manifest.parent_revision
            and matches[0].ordinal == manifest.ordinal
            and matches[0].tree_digest == manifest.tree_digest
            and matches[0].change_digest == manifest.change_digest
        )


class PostgresWorkflowRepository:
    """DB-API repository; caller supplies a PostgreSQL connection factory."""

    def __init__(self, connect) -> None:
        self._connect = connect

    @contextmanager
    def _transaction(self) -> Iterator[object]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def add_intent(self, intent: PublicationIntent) -> None:
        with self._transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 1))",
                (intent.intent_token,),
            )
            cursor.execute(
                "INSERT INTO hosted_publication_intent (intent_id,intent_token,kind,campaign_id,revision_id,parent_revision,ordinal,tree_digest,change_digest,status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (intent_id) DO NOTHING",
                (intent.intent_id, intent.intent_token, intent.kind.value, intent.campaign_id, intent.revision_id, intent.parent_revision, intent.ordinal, intent.tree_digest, intent.change_digest, intent.status.value),
            )

    def matching_intents(self, token: str) -> tuple[PublicationIntent, ...]:
        with self._transaction() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT intent_id,intent_token,kind,campaign_id,revision_id,parent_revision,ordinal,tree_digest,change_digest,status FROM hosted_publication_intent WHERE intent_token=%s ORDER BY intent_id", (token,))
            return tuple(
                PublicationIntent(
                    row[0], row[1], PublicationKind(row[2]), row[3], row[4],
                    row[5], row[6], row[7], row[8], IntentStatus(row[9]),
                )
                for row in cursor.fetchall()
            )

    def finalize_head(self, intent: PublicationIntent) -> bool:
        with self._transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 1))",
                (intent.intent_token,),
            )
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (intent.campaign_id,),
            )
            cursor.execute(
                "SELECT intent_id,kind,campaign_id,revision_id,parent_revision,ordinal,tree_digest,change_digest,status FROM hosted_publication_intent WHERE intent_token=%s ORDER BY intent_id FOR UPDATE",
                (intent.intent_token,),
            )
            matches = cursor.fetchall()
            if len(matches) != 1:
                raise PublicationIntentError(
                    "publication intent token is ambiguous"
                )
            exact = matches[0]
            expected = (
                intent.intent_id,
                intent.kind.value,
                intent.campaign_id,
                intent.revision_id,
                intent.parent_revision,
                intent.ordinal,
                intent.tree_digest,
                intent.change_digest,
            )
            if tuple(exact[:8]) != expected:
                raise PublicationIntentError(
                    "publication intent binding mismatch"
                )
            if exact[8] == IntentStatus.FINALIZED.value:
                return False
            if exact[8] != IntentStatus.PENDING.value:
                raise PublicationIntentError("publication intent is not pending")
            cursor.execute("SELECT revision_id, ordinal FROM hosted_campaign_head WHERE campaign_id=%s FOR UPDATE", (intent.campaign_id,))
            current = cursor.fetchone()
            expected = None if current is None else current[0]
            expected_ordinal = 1 if current is None else current[1] + 1
            if expected != intent.parent_revision or expected_ordinal != intent.ordinal:
                raise StaleHeadError("campaign head compare-and-swap failed")
            cursor.execute("UPDATE hosted_publication_intent SET status='finalized' WHERE intent_id=%s AND status='pending'", (intent.intent_id,))
            if cursor.rowcount == 0:
                return False
            cursor.execute("INSERT INTO hosted_campaign_head(campaign_id,revision_id,ordinal) VALUES(%s,%s,%s) ON CONFLICT(campaign_id) DO UPDATE SET revision_id=EXCLUDED.revision_id, ordinal=EXCLUDED.ordinal", (intent.campaign_id, intent.revision_id, intent.ordinal))
            return True

    def finalize_editor_publication(self, intent: PublicationIntent, proposal_id: str,
                                    proposal_version: int, expected_editor_version: int,
                                    editor_metadata: dict) -> bool:
        """Commit editor terminal state, workflow CAS, intent, and head together."""
        with self._transaction() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 1))", (intent.intent_token,))
            cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (intent.campaign_id,))
            cursor.execute("SELECT intent_id,kind,campaign_id,revision_id,parent_revision,ordinal,tree_digest,change_digest,status FROM hosted_publication_intent WHERE intent_token=%s ORDER BY intent_id FOR UPDATE", (intent.intent_token,))
            rows = cursor.fetchall()
            if len(rows) != 1 or tuple(rows[0][:8]) != (intent.intent_id, intent.kind.value, intent.campaign_id, intent.revision_id, intent.parent_revision, intent.ordinal, intent.tree_digest, intent.change_digest):
                raise PublicationIntentError("publication intent binding mismatch")
            if rows[0][8] == IntentStatus.FINALIZED.value:
                return False
            if rows[0][8] != IntentStatus.PENDING.value:
                raise PublicationIntentError("publication intent is not pending")
            cursor.execute("SELECT version FROM hosted_editor_workflow WHERE campaign_id=%s FOR UPDATE", (intent.campaign_id,))
            workflow = cursor.fetchone()
            if workflow is None or workflow[0] != expected_editor_version:
                raise StaleHeadError("editor workflow compare-and-swap failed")
            cursor.execute("SELECT revision_id,ordinal FROM hosted_campaign_head WHERE campaign_id=%s FOR UPDATE", (intent.campaign_id,))
            current = cursor.fetchone()
            expected_head = None if current is None else current[0]
            expected_ordinal = 1 if current is None else current[1] + 1
            if expected_head != intent.parent_revision or expected_ordinal != intent.ordinal:
                raise StaleHeadError("campaign head compare-and-swap failed")
            cursor.execute("SELECT proposal_id,version,campaign_id,base_revision,changes,diff_digest,payload_digest,status,generation_id,source_revision,source_set_digest,terminal_draft_digest,published_revision_id,editor_metadata FROM hosted_proposal_version WHERE proposal_id=%s AND version=%s FOR UPDATE", (proposal_id, proposal_version))
            proposal = cursor.fetchone()
            if proposal is None or proposal[2] != intent.campaign_id or proposal[3] != intent.parent_revision or proposal[7] != "approving":
                raise PublicationIntentError("editor proposal is not approving")
            cursor.execute("UPDATE hosted_proposal_version SET status='published',publication_intent_token=%s,published_revision_id=%s,result_digest=%s,editor_metadata=%s::jsonb WHERE proposal_id=%s AND version=%s AND status='approving'", (intent.intent_token, intent.revision_id, intent.tree_digest, json.dumps(editor_metadata), proposal_id, proposal_version))
            if cursor.rowcount != 1:
                raise PublicationIntentError("editor proposal state changed during publication")
            cursor.execute("UPDATE hosted_editor_workflow SET version=%s WHERE campaign_id=%s AND version=%s", (expected_editor_version + 1, intent.campaign_id, expected_editor_version))
            if cursor.rowcount != 1:
                raise StaleHeadError("editor workflow compare-and-swap failed")
            cursor.execute("UPDATE hosted_publication_intent SET status='finalized' WHERE intent_id=%s AND status='pending'", (intent.intent_id,))
            if cursor.rowcount != 1:
                raise PublicationIntentError("publication intent changed during finalization")
            cursor.execute("INSERT INTO hosted_campaign_head(campaign_id,revision_id,ordinal) VALUES(%s,%s,%s) ON CONFLICT(campaign_id) DO UPDATE SET revision_id=EXCLUDED.revision_id,ordinal=EXCLUDED.ordinal", (intent.campaign_id, intent.revision_id, intent.ordinal))
            cursor.execute("INSERT INTO hosted_proposal_audit(proposal_id,version,status,diff_digest,payload_digest,event,publication_intent_token,published_revision_id,result_digest) VALUES(%s,%s,'published',%s,%s,'published',%s,%s,%s)", (proposal_id, proposal_version, proposal[5], proposal[6], intent.intent_token, intent.revision_id, intent.tree_digest))
            return True

    def finalize_editor_rejection(self, campaign_id: str, proposal_id: str, proposal_version: int,
                                  expected_editor_version: int, editor_metadata: dict) -> bool:
        """Commit a rejection and its editor workflow CAS in one transaction."""
        with self._transaction() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (campaign_id,))
            cursor.execute("SELECT version FROM hosted_editor_workflow WHERE campaign_id=%s FOR UPDATE", (campaign_id,))
            workflow = cursor.fetchone()
            if workflow is None or workflow[0] != expected_editor_version:
                return False
            cursor.execute("SELECT proposal_id,version,campaign_id,base_revision,changes,diff_digest,payload_digest,status,generation_id,source_revision,source_set_digest,terminal_draft_digest,published_revision_id,editor_metadata FROM hosted_proposal_version WHERE proposal_id=%s AND version=%s FOR UPDATE", (proposal_id, proposal_version))
            proposal = cursor.fetchone()
            if proposal is None or proposal[2] != campaign_id or proposal[7] != "draft":
                return False
            cursor.execute("UPDATE hosted_proposal_version SET status='rejected',editor_metadata=%s::jsonb WHERE proposal_id=%s AND version=%s AND status='draft'", (json.dumps(editor_metadata), proposal_id, proposal_version))
            if cursor.rowcount != 1:
                return False
            cursor.execute("UPDATE hosted_editor_workflow SET version=%s WHERE campaign_id=%s AND version=%s", (expected_editor_version + 1, campaign_id, expected_editor_version))
            if cursor.rowcount != 1:
                raise StaleHeadError("editor workflow compare-and-swap failed")
            cursor.execute("INSERT INTO hosted_proposal_audit(proposal_id,version,status,diff_digest,payload_digest,event) VALUES(%s,%s,'rejected',%s,%s,'rejected')", (proposal_id, proposal_version, proposal[5], proposal[6]))
            return True

    def quarantine_intent(self, intent_id: str) -> None:
        with self._transaction() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE hosted_publication_intent SET status='quarantined' WHERE intent_id=%s AND status<>'finalized'", (intent_id,))

    def head(self, campaign_id: str) -> str | None:
        with self._transaction() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT revision_id FROM hosted_campaign_head WHERE campaign_id=%s", (campaign_id,))
            row = cursor.fetchone()
            return row[0] if row else None

    def publication_eligible(self, manifest) -> bool:
        with self._transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT intent_id,kind,campaign_id,revision_id,parent_revision,ordinal,tree_digest,change_digest,status FROM hosted_publication_intent WHERE intent_token=%s ORDER BY intent_id",
                (manifest.publication_intent_token,),
            )
            rows = cursor.fetchall()
            return (
                len(rows) == 1
                and rows[0][2] == manifest.campaign_id
                and rows[0][3] == manifest.revision_id
                and rows[0][4] == manifest.parent_revision
                and rows[0][5] == manifest.ordinal
                and rows[0][6] == manifest.tree_digest
                and rows[0][7] == manifest.change_digest
                and rows[0][8] == IntentStatus.FINALIZED.value
            )
