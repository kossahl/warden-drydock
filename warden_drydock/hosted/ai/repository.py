from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
import uuid
from typing import Iterator
import threading

from .models import Action, Capture, CaptureType, GenerationRecord, GenerationRequest, LiveSession, ProviderConsent, SourceEnvelope, SourceExcerpt, StreamEvent


class InMemoryAIRepository:
    """Transactional reference repository used by services and deterministic tests."""

    def __init__(self) -> None:
        self.sources: dict[str, SourceEnvelope] = {}
        self.generations: dict[str, GenerationRecord] = {}
        self.sessions: dict[str, LiveSession] = {}
        self.dispatch_log: list[str] = []
        self._provider_consent: ProviderConsent | None = None
        self._lock = threading.RLock()

    @contextmanager
    def transaction(self) -> Iterator["InMemoryAIRepository"]:
        snapshot = deepcopy((self.sources, self.generations, self.sessions, self.dispatch_log, self._provider_consent))
        try:
            yield self
        except Exception:
            self.sources, self.generations, self.sessions, self.dispatch_log, self._provider_consent = snapshot
            raise

    def consent(self) -> ProviderConsent | None:
        return self._provider_consent

    def set_consent(self, consent: ProviderConsent) -> None:
        self._provider_consent = consent

    def get_generation(self, generation_id: str) -> GenerationRecord | None:
        return self.generations.get(generation_id)

    def reserve_generation(self, record: GenerationRecord) -> bool:
        generation_id = record.request.generation_id
        with self._lock:
            existing = self.generations.get(generation_id)
            if existing is not None:
                if existing.request != record.request:
                    raise ValueError("idempotency_digest_conflict")
                return False
            self.sources[generation_id] = record.request.envelope
            self.generations[generation_id] = record
            return True

    def save_generation(self, record: GenerationRecord) -> None:
        with self._lock:
            self.generations[record.request.generation_id] = record

    def finalize_generation(self, record: GenerationRecord, event: StreamEvent, status: str) -> None:
        with self._lock:
            record.events.append(event)
            record.terminal_status = status
            self.generations[record.request.generation_id] = record

    def active_session(self, campaign_id: str) -> LiveSession | None:
        return next((item for item in self.sessions.values() if item.campaign_id == campaign_id and item.mode == "active"), None)

    def get_session(self, session_id: str) -> LiveSession:
        return self.sessions[session_id]

    def create_session(self, session: LiveSession) -> None:
        self.sessions[session.session_id] = session

    def save_session(self, session: LiveSession, *, expected_workflow_version: int | None = None, expected_epoch: int | None = None) -> None:
        current = self.sessions.get(session.session_id)
        if current is not session and current is not None:
            if expected_workflow_version is not None and current.workflow_version != expected_workflow_version:
                raise ValueError("stale_workflow_version")
            if expected_epoch is not None and current.controller_epoch != expected_epoch:
                raise ValueError("stale_controller_epoch")
        self.sessions[session.session_id] = session

    def update_reported_head(self, session_id: str, revision_id: str) -> None:
        self.sessions[session_id].reported_head_revision = revision_id

    def draft_generation_count(self, campaign_id: str, revision_id: str) -> int:
        return sum(
            1 for item in self.generations.values()
            if item.request.campaign_id == campaign_id
            and item.request.revision_id == revision_id
            and item.terminal_status == "complete"
        )


class PostgresAIRepository:
    """PostgreSQL implementation of the same provider/live repository contract."""

    def __init__(self, connect) -> None:
        self._connect = connect
        self.dispatch_log: list[str] = []

    @contextmanager
    def transaction(self):
        connection = self._connect()
        try:
            with connection:
                yield self
        finally:
            connection.close()

    def consent(self) -> ProviderConsent | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT credential_revision_fingerprint,adapter_version,endpoint_id,region,storage_mode,retrieval_policy_version,notice_digest,revoked_at IS NULL FROM hosted_provider_consent ORDER BY consented_at DESC LIMIT 1")
            row = cursor.fetchone()
        return ProviderConsent(*row) if row else None

    def set_consent(self, consent: ProviderConsent) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended('hosted_provider_consent', 0))")
            cursor.execute("SELECT credential_revision_fingerprint,adapter_version,endpoint_id,region,storage_mode,retrieval_policy_version,notice_digest,revoked_at IS NULL FROM hosted_provider_consent WHERE revoked_at IS NULL ORDER BY consented_at DESC LIMIT 1")
            row = cursor.fetchone()
            if row is not None and ProviderConsent(*row) == consent:
                return
            cursor.execute("UPDATE hosted_provider_consent SET revoked_at=now() WHERE revoked_at IS NULL")
            cursor.execute("INSERT INTO hosted_provider_consent(consent_id,credential_revision_fingerprint,adapter_version,endpoint_id,region,storage_mode,retrieval_policy_version,notice_digest) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                ("consent_" + uuid.uuid4().hex[:24], consent.credential_revision_fingerprint, consent.adapter_version, consent.endpoint_id, consent.region, consent.storage_mode, consent.retrieval_policy_version, consent.notice_digest))

    @staticmethod
    def _envelope(value: dict) -> SourceEnvelope:
        excerpts = tuple(SourceExcerpt(**item) for item in value["excerpts"])
        return SourceEnvelope(value["campaign_id"], value["revision_id"], excerpts, value.get("session_id"), value["retrieval_policy_version"])

    def get_generation(self, generation_id: str) -> GenerationRecord | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT campaign_id,revision_id,session_id,action,prompt,source_envelope,status,terminal_draft,focus_record_id,focus_content_digest FROM hosted_ai_generation WHERE generation_id=%s", (generation_id,))
            row = cursor.fetchone()
            if not row:
                return None
            envelope = self._envelope(row[5])
            if envelope.campaign_id != row[0] or envelope.revision_id != row[1] or envelope.session_id != row[2]:
                raise ValueError("unsafe_binding")
            record = GenerationRecord(GenerationRequest(generation_id, row[0], row[1], Action(row[3]), row[4], envelope, row[8], row[9]), terminal_status=None if row[6] == "pending" else row[6], terminal_content=row[7] or "")
            cursor.execute("SELECT sequence,event_type,payload FROM hosted_ai_stream_event WHERE generation_id=%s ORDER BY sequence", (generation_id,))
            record.events = [StreamEvent(item[0], item[1], item[2].get("draft_fragment"), item[2].get("retryable")) for item in cursor.fetchall()]
            return record

    def reserve_generation(self, record: GenerationRecord) -> bool:
        request = record.request
        envelope = request.envelope
        request_binding = {
            "action": request.action.value,
            "campaign_id": request.campaign_id,
            "prompt": request.prompt,
            "revision_id": request.revision_id,
            "session_id": envelope.session_id,
            "source_set_digest": envelope.source_set_digest,
        }
        if request.focus_record_id is not None:
            request_binding.update(
                focus_record_id=request.focus_record_id,
                focus_content_digest=request.focus_content_digest,
            )
        request_digest = __import__("hashlib").sha256(
            json.dumps(
                request_binding, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        encoded = {"campaign_id": envelope.campaign_id, "revision_id": envelope.revision_id, "session_id": envelope.session_id, "retrieval_policy_version": envelope.retrieval_policy_version, "excerpts": [item.__dict__ for item in envelope.excerpts]}
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("INSERT INTO hosted_ai_generation(generation_id,campaign_id,revision_id,session_id,action,prompt,request_digest,source_set_digest,source_envelope,status,focus_record_id,focus_content_digest) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,'pending',%s,%s) ON CONFLICT(generation_id) DO NOTHING",
                (request.generation_id, request.campaign_id, request.revision_id, envelope.session_id, request.action.value, request.prompt, request_digest, envelope.source_set_digest, json.dumps(encoded), request.focus_record_id, request.focus_content_digest))
            if cursor.rowcount == 0:
                cursor.execute("SELECT request_digest FROM hosted_ai_generation WHERE generation_id=%s", (request.generation_id,))
                if cursor.fetchone() != (request_digest,):
                    raise ValueError("idempotency_digest_conflict")
                return False
            return True

    def save_generation(self, record: GenerationRecord) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            for event in record.events:
                payload = {"draft_fragment": event.draft_fragment, "retryable": event.retryable}
                cursor.execute("INSERT INTO hosted_ai_stream_event(generation_id,sequence,event_type,payload) VALUES(%s,%s,%s,%s::jsonb) ON CONFLICT DO NOTHING", (record.request.generation_id, event.sequence, event.event_type, json.dumps(payload)))
                if cursor.rowcount == 0:
                    cursor.execute("SELECT event_type,payload FROM hosted_ai_stream_event WHERE generation_id=%s AND sequence=%s", (record.request.generation_id, event.sequence))
                    stored = cursor.fetchone()
                    if stored is None or stored[0] != event.event_type or stored[1] != payload:
                        raise ValueError("stream_sequence_conflict")
            cursor.execute("UPDATE hosted_ai_generation SET status=%s,terminal_draft=%s WHERE generation_id=%s", (record.terminal_status or "pending", record.terminal_content or None, record.request.generation_id))

    def finalize_generation(self, record: GenerationRecord, event: StreamEvent, status: str) -> None:
        payload = {"draft_fragment": event.draft_fragment, "retryable": event.retryable}
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("INSERT INTO hosted_ai_stream_event(generation_id,sequence,event_type,payload) VALUES(%s,%s,%s,%s::jsonb) ON CONFLICT DO NOTHING", (record.request.generation_id, event.sequence, event.event_type, json.dumps(payload)))
            if cursor.rowcount == 0:
                cursor.execute("SELECT event_type,payload FROM hosted_ai_stream_event WHERE generation_id=%s AND sequence=%s", (record.request.generation_id, event.sequence))
                stored = cursor.fetchone()
                if stored is None or stored[0] != event.event_type or stored[1] != payload:
                    raise ValueError("stream_sequence_conflict")
            cursor.execute("UPDATE hosted_ai_generation SET status=%s,terminal_draft=%s WHERE generation_id=%s AND status='pending'", (status, record.terminal_content or None, record.request.generation_id))
            if cursor.rowcount != 1:
                raise ValueError("stream_sequence_conflict")
        record.events.append(event)
        record.terminal_status = status

    def active_session(self, campaign_id: str) -> LiveSession | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT session_id FROM hosted_live_session WHERE campaign_id=%s AND mode='active'", (campaign_id,))
            row = cursor.fetchone()
        return self.get_session(row[0]) if row else None

    def get_session(self, session_id: str) -> LiveSession:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT campaign_id,base_revision,reported_head_revision,workflow_version,controller_epoch,controller_id,mode FROM hosted_live_session WHERE session_id=%s", (session_id,))
            row = cursor.fetchone()
            if not row:
                raise KeyError(session_id)
            session = LiveSession(session_id, *row)
            cursor.execute("SELECT event_id,device_id,operation_id,device_order,capture_type,content,payload_digest FROM hosted_live_capture WHERE session_id=%s ORDER BY device_order,event_id", (session_id,))
            for item in cursor.fetchall():
                capture = Capture(item[0], item[1], item[2], item[3], CaptureType(item[4]), item[5], item[6])
                session.captures.append(capture)
            cursor.execute("SELECT device_id,operation_id,payload_digest FROM hosted_live_receipt WHERE session_id=%s", (session_id,))
            session.receipts = {(item[0], item[1]): item[2] for item in cursor.fetchall()}
            return session

    def create_session(self, session: LiveSession) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("INSERT INTO hosted_live_session(session_id,campaign_id,base_revision,reported_head_revision,workflow_version,controller_epoch,controller_id,mode) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)", (session.session_id, session.campaign_id, session.base_revision, session.reported_head_revision, session.workflow_version, session.controller_epoch, session.controller_id, session.mode))

    def save_session(self, session: LiveSession, *, expected_workflow_version: int | None = None, expected_epoch: int | None = None) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            clauses = ["session_id=%s"]
            bindings: list[object] = [session.session_id]
            if expected_workflow_version is not None:
                clauses.append("workflow_version=%s")
                bindings.append(expected_workflow_version)
            if expected_epoch is not None:
                clauses.append("controller_epoch=%s")
                bindings.append(expected_epoch)
            cursor.execute("UPDATE hosted_live_session SET reported_head_revision=%s,workflow_version=%s,controller_epoch=%s,controller_id=%s,mode=%s WHERE " + " AND ".join(clauses), (session.reported_head_revision, session.workflow_version, session.controller_epoch, session.controller_id, session.mode, *bindings))
            if cursor.rowcount != 1:
                raise ValueError("stale_workflow_version")
            for item in session.captures:
                cursor.execute("INSERT INTO hosted_live_capture(session_id,event_id,device_id,operation_id,device_order,capture_type,payload_digest,content) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING", (session.session_id, item.event_id, item.device_id, item.operation_id, item.device_order, item.capture_type.value, item.payload_digest, item.text))
            for (device_id, operation_id), payload_digest in session.receipts.items():
                cursor.execute("INSERT INTO hosted_live_receipt(session_id,device_id,operation_id,payload_digest) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING", (session.session_id, device_id, operation_id, payload_digest))
                if cursor.rowcount == 0:
                    cursor.execute("SELECT payload_digest FROM hosted_live_receipt WHERE session_id=%s AND device_id=%s AND operation_id=%s", (session.session_id, device_id, operation_id))
                    row = cursor.fetchone()
                    if row is None or row[0] != payload_digest:
                        raise ValueError("idempotency_digest_conflict")

    def update_reported_head(self, session_id: str, revision_id: str) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE hosted_live_session SET reported_head_revision=%s WHERE session_id=%s", (revision_id, session_id))
            if cursor.rowcount != 1:
                raise KeyError(session_id)

    def draft_generation_count(self, campaign_id: str, revision_id: str) -> int:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM hosted_ai_generation "
                "WHERE campaign_id=%s AND revision_id=%s AND status='complete'",
                (campaign_id, revision_id),
            )
            return cursor.fetchone()[0]
