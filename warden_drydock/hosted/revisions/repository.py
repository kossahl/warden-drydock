from __future__ import annotations

from contextlib import contextmanager
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
