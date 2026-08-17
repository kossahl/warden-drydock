from __future__ import annotations

from .models import Capture, CaptureType, LiveSession, canonical_digest


class StaleController(PermissionError):
    pass


class StaleWorkflow(PermissionError):
    pass


class LiveSessionService:
    def __init__(self, repository) -> None:
        self.repository = repository
        self._enabled = True

    def disable(self) -> None:
        self._enabled = False

    def start(self, session_id: str, campaign_id: str, head_revision: str, controller_id: str) -> LiveSession:
        if not self._enabled:
            raise RuntimeError("live feature is disabled")
        session = LiveSession(session_id, campaign_id, head_revision, head_revision, controller_id=controller_id)
        existing = None
        try:
            existing = self.repository.get_session(session_id)
        except KeyError:
            pass
        if existing is not None:
            if existing.campaign_id != campaign_id or existing.base_revision != head_revision:
                raise ValueError("idempotency_digest_conflict")
            return existing
        active = self.repository.active_session(campaign_id)
        if active is not None:
            raise ValueError("active_session_conflict")
        self.repository.create_session(session)
        return session

    def observe(self, session_id: str, *, reported_head_revision: str | None = None) -> LiveSession:
        session = self.repository.get_session(session_id)
        if reported_head_revision:
            self.repository.update_reported_head(session_id, reported_head_revision)
            session.reported_head_revision = reported_head_revision
        return session

    def takeover(self, session_id: str, controller_id: str, expected_epoch: int, expected_workflow_version: int) -> LiveSession:
        session = self.repository.get_session(session_id)
        self._require_epoch(session, expected_epoch)
        if session.mode != "active":
            raise StaleWorkflow("stale_workflow_version")
        if expected_workflow_version is not None and session.workflow_version != expected_workflow_version:
            raise StaleWorkflow("stale_workflow_version")
        session.controller_epoch += 1
        session.controller_id = controller_id
        session.workflow_version += 1
        self.repository.save_session(session, expected_workflow_version=expected_workflow_version, expected_epoch=expected_epoch)
        return session

    def capture(self, session_id: str, controller_id: str, controller_epoch: int, expected_workflow_version: int, *, event_id: str, device_id: str, operation_id: str, device_order: int, capture_type: CaptureType, text: str) -> str:
        session = self.repository.get_session(session_id)
        self._require_controller(session, controller_id, controller_epoch)
        if session.mode != "active":
            raise StaleWorkflow("stale_workflow_version")
        if session.mode != "active":
            raise StaleWorkflow("stale_workflow_version")
        if session.workflow_version != expected_workflow_version:
            raise StaleWorkflow("stale_workflow_version")
        digest = canonical_digest({"base_revision": session.base_revision, "capture_type": capture_type.value, "device_order": device_order, "event_id": event_id, "text": text})
        if device_order < 1 or not text:
            raise ValueError("unsafe_binding")
        key = (device_id, operation_id)
        prior = session.receipts.get(key)
        if prior is not None:
            if prior != digest:
                raise ValueError("idempotency_digest_conflict")
            return "exact_replay"
        if any(item.event_id == event_id for item in session.captures):
            raise ValueError("idempotency_digest_conflict")
        session.captures.append(Capture(event_id, device_id, operation_id, device_order, capture_type, text, digest))
        session.receipts[key] = digest
        session.workflow_version += 1
        self.repository.save_session(session, expected_workflow_version=expected_workflow_version, expected_epoch=controller_epoch)
        return "accepted"

    def grounding_facts(self, session_id: str) -> tuple[Capture, ...]:
        session = self.repository.get_session(session_id)
        return tuple(item for item in session.captures if item.capture_type is CaptureType.CONFIRMED_FACT)

    def end(self, session_id: str, controller_id: str, controller_epoch: int, expected_workflow_version: int, *, device_id: str, operation_id: str) -> LiveSession:
        session = self.repository.get_session(session_id)
        digest = canonical_digest({"base_revision": session.base_revision, "event_type": "end_intent", "operation_id": operation_id})
        prior = session.receipts.get((device_id, operation_id))
        if prior is not None:
            if prior != digest:
                raise ValueError("idempotency_digest_conflict")
            return session
        self._require_controller(session, controller_id, controller_epoch)
        if session.mode != "active":
            raise StaleWorkflow("stale_workflow_version")
        if session.workflow_version != expected_workflow_version:
            raise StaleWorkflow("stale_workflow_version")
        session.mode = "ended_review_pending"
        session.receipts[(device_id, operation_id)] = digest
        session.workflow_version += 1
        self.repository.save_session(session, expected_workflow_version=expected_workflow_version, expected_epoch=controller_epoch)
        return session

    @staticmethod
    def _require_epoch(session: LiveSession, expected_epoch: int) -> None:
        if session.controller_epoch != expected_epoch:
            raise StaleController("stale_controller_epoch")

    @classmethod
    def _require_controller(cls, session: LiveSession, controller_id: str, epoch: int) -> None:
        cls._require_epoch(session, epoch)
        if session.controller_id != controller_id:
            raise StaleController("stale_controller_epoch")
