from __future__ import annotations

import threading

from .models import Capture, CaptureType, LiveEndBarrier, LiveSession, live_operation_digest


class StaleController(PermissionError):
    pass


class StaleWorkflow(PermissionError):
    pass


class LiveSessionService:
    def __init__(self, repository) -> None:
        self.repository = repository
        self._enabled = True
        self._lock = threading.RLock()

    def disable(self) -> None:
        self._enabled = False

    def start(self, session_id: str, campaign_id: str, head_revision: str, controller_id: str) -> LiveSession:
        with self._lock:
            if not self._enabled:
                raise RuntimeError("live feature is disabled")
            session = LiveSession(session_id, campaign_id, head_revision, head_revision, controller_id=controller_id)
            existing = None
            try:
                existing = self.repository.get_session(session_id)
            except KeyError:
                pass
            if existing is not None:
                # P2-4: bind the controller identity into start replay. A second start
                # with a different controller must not silently return the existing session.
                if (
                    existing.campaign_id != campaign_id
                    or existing.base_revision != head_revision
                    or existing.controller_id != controller_id
                ):
                    raise ValueError("idempotency_digest_conflict")
                return existing
            active = self.repository.active_session(campaign_id)
            if active is not None:
                raise ValueError("active_session_conflict")
            try:
                self.repository.create_session(session)
            except ValueError:
                # Postgres raises for a unique active-campaign violation under the
                # one-active-session index; surface the advertised conflict.
                raise ValueError("active_session_conflict")
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

    def capture(self, session_id: str, controller_id: str, controller_epoch: int, expected_workflow_version: int, *, event_id: str, device_id: str, operation_id: str, device_order: int, capture_type: CaptureType, text: str, record_id: str | None = None) -> str:
        with self._lock:
            session = self.repository.get_session(session_id)
            if device_order < 1 or not text:
                raise ValueError("unsafe_binding")
            digest = live_operation_digest(
                campaign_id=session.campaign_id,
                base_revision=session.base_revision,
                session_id=session_id,
                controller_id=controller_id,
                controller_epoch=controller_epoch,
                workflow_version=expected_workflow_version,
                event_type=capture_type.value,
                event_id=event_id,
                device_id=device_id,
                operation_id=operation_id,
                device_order=device_order,
                text=text,
                record_id=record_id,
            )
            key = (device_id, operation_id)
            prior = session.receipts.get(key)
            if prior is not None:
                if prior != digest:
                    raise ValueError("idempotency_digest_conflict")
                return "exact_replay"
            self._require_controller(session, controller_id, controller_epoch)
            if session.mode != "active":
                raise StaleWorkflow("stale_workflow_version")
            if session.workflow_version != expected_workflow_version:
                raise StaleWorkflow("stale_workflow_version")
            # Orders are a per-device monotonic sequence. Do not allow a new
            # operation to reuse or reverse a device's order; gaps are valid.
            # remain idempotent.
            device_orders = [
                item.device_order for item in session.captures
                if item.device_id == device_id
            ]
            if device_orders and device_order <= max(device_orders):
                raise ValueError("unsafe_binding")
            if any(item.event_id == event_id for item in session.captures):
                raise ValueError("idempotency_digest_conflict")
            session.captures.append(Capture(event_id, device_id, operation_id, device_order, capture_type, text, digest, record_id))
            session.receipts[key] = digest
            session.workflow_version += 1
            try:
                self.repository.save_session(session, expected_workflow_version=expected_workflow_version, expected_epoch=controller_epoch)
            except ValueError as exc:
                if str(exc) == "stale_workflow_version":
                    # P2-D: optimistic concurrency loss. Reload the persisted
                    # receipt set; if the identical (device_id, operation_id) now
                    # holds the identical digest, another request committed it, so
                    # this is an exact replay.
                    fresh = self.repository.get_session(session_id)
                    persisted = fresh.receipts.get(key)
                    if persisted == digest:
                        return "exact_replay"
                raise
            return "accepted"

    def grounding_facts(self, session_id: str) -> tuple[Capture, ...]:
        session = self.repository.get_session(session_id)
        return tuple(item for item in session.captures if item.capture_type is CaptureType.CONFIRMED_FACT)

    def end(self, session_id: str, controller_id: str, controller_epoch: int, expected_workflow_version: int, *, device_id: str, operation_id: str, required_operation_ids: tuple[tuple[str, str], ...] = ()) -> LiveSession:
        with self._lock:
            session = self.repository.get_session(session_id)
            # Barrier identities are (device, operation), not operation alone.
            required_set = set(required_operation_ids)
            if (device_id, operation_id) in required_set:
                # P1-A: the end operation must not self-certify readiness. Only
                # capture operations may be required for proposal readiness.
                raise ValueError("unsafe_binding")
            digest = live_operation_digest(
                campaign_id=session.campaign_id,
                base_revision=session.base_revision,
                session_id=session_id,
                controller_id=controller_id,
                controller_epoch=controller_epoch,
                workflow_version=expected_workflow_version,
                event_type="end_intent",
                event_id=None,
                device_id=device_id,
                operation_id=operation_id,
                device_order=None,
                text="",
                required_operation_ids=sorted(required_set),
            )
            prior = session.receipts.get((device_id, operation_id))
            if prior is not None:
                # A changed required set (or any other bound state) yields a
                # different digest, so a replay with a changed barrier is a conflict.
                if prior != digest:
                    raise ValueError("idempotency_digest_conflict")
                return session
            self._require_controller(session, controller_id, controller_epoch)
            if session.mode != "active":
                raise StaleWorkflow("stale_workflow_version")
            if session.workflow_version != expected_workflow_version:
                raise StaleWorkflow("stale_workflow_version")
            # Fail closed ALWAYS: the barrier must not hide any already-acknowledged
            # capture, even when the required set is empty.
            current_ops = set(session.receipts)
            hidden = current_ops - required_set
            if hidden:
                raise ValueError("live_unaccepted_barrier")
            # P1-A: readiness is computed EXCLUSIVELY against the capture receipt set
            # BEFORE the end receipt is added, so the end operation can never
            # self-confirm readiness.
            accepted_ops = set(current_ops)
            ready_for_proposal = required_set <= accepted_ops
            session.mode = "ended_review_pending"
            session.receipts[(device_id, operation_id)] = digest
            session.workflow_version += 1
            session.end_barrier = LiveEndBarrier(
                end_operation_id=operation_id,
                end_device_id=device_id,
                required_operation_ids=tuple(sorted(required_set)),
                ready_for_proposal=ready_for_proposal,
            )
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
