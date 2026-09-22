from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import threading


class ReceiptConflict(ValueError):
    pass


@dataclass(frozen=True)
class CampaignPlayer:
    player_id: str
    name: str
    status: str = "active"


@dataclass(frozen=True)
class CampaignProfile:
    campaign_id: str
    created_at: datetime
    display_timezone: str = "UTC"
    players: tuple[CampaignPlayer, ...] = ()

    @property
    def active_player_count(self) -> int:
        return sum(item.status not in {"inactive", "archived"} for item in self.players)


class InMemoryCampaignProfileRepository:
    def __init__(self) -> None:
        self.profiles: dict[str, CampaignProfile] = {}

    def create(self, profile: CampaignProfile) -> None:
        existing = self.profiles.get(profile.campaign_id)
        if existing is not None:
            if (
                existing.display_timezone != profile.display_timezone
                or existing.players != profile.players
            ):
                raise ValueError("campaign_profile_conflict")
            return
        self.profiles[profile.campaign_id] = profile

    def get(self, campaign_id: str) -> CampaignProfile | None:
        return self.profiles.get(campaign_id)

    def delete(self, campaign_id: str) -> None:
        self.profiles.pop(campaign_id, None)

    def prune_orphans(self, campaign_ids: set[str]) -> None:
        for campaign_id in tuple(self.profiles):
            if campaign_id not in campaign_ids:
                del self.profiles[campaign_id]


class PostgresCampaignProfileRepository:
    def __init__(self, connect) -> None:
        self._connect = connect

    def create(self, profile: CampaignProfile) -> None:
        encoded_players = json.dumps(
            [item.__dict__ for item in profile.players],
            separators=(",", ":"), sort_keys=True,
        )
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO hosted_campaign_profile(campaign_id,created_at,display_timezone,players) "
                "VALUES(%s,%s,%s,%s::jsonb) ON CONFLICT(campaign_id) DO NOTHING",
                (profile.campaign_id, profile.created_at, profile.display_timezone, encoded_players),
            )
            cursor.execute(
                "SELECT created_at,display_timezone,players FROM hosted_campaign_profile WHERE campaign_id=%s",
                (profile.campaign_id,),
            )
            row = cursor.fetchone()
        if row is None:
            raise ValueError("campaign_profile_unavailable")
        existing = self._decode(profile.campaign_id, row)
        if (
            existing.display_timezone != profile.display_timezone
            or existing.players != profile.players
        ):
            raise ValueError("campaign_profile_conflict")

    def get(self, campaign_id: str) -> CampaignProfile | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT created_at,display_timezone,players FROM hosted_campaign_profile WHERE campaign_id=%s",
                (campaign_id,),
            )
            row = cursor.fetchone()
        return self._decode(campaign_id, row) if row is not None else None

    def delete(self, campaign_id: str) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM hosted_campaign_profile WHERE campaign_id=%s", (campaign_id,))

    def prune_orphans(self, campaign_ids: set[str]) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM hosted_campaign_profile WHERE campaign_id <> ALL(%s)",
                (list(campaign_ids),),
            )

    @staticmethod
    def _decode(campaign_id: str, row) -> CampaignProfile:
        players = json.loads(row[2]) if isinstance(row[2], str) else row[2]
        return CampaignProfile(
            campaign_id,
            row[0].astimezone(timezone.utc),
            row[1],
            tuple(CampaignPlayer(item["player_id"], item["name"], item["status"]) for item in players),
        )


class InMemoryHTTPRepository:
    """Exact-replay receipts used by deterministic tests and local development."""

    def __init__(self) -> None:
        self._receipts: dict[tuple[str, str], tuple[str, int | None, dict | None]] = {}
        self._lock = threading.RLock()

    def replay(self, operation: str, key: str, payload_digest: str):
        with self._lock:
            stored = self._receipts.get((operation, key))
            if stored is None:
                return None
            if stored[0] != payload_digest:
                raise ReceiptConflict("idempotency_digest_conflict")
            if stored[1] is None:
                return None
            if not isinstance(stored[2], dict) or stored[2].get("contract_version") not in ({1, 2} if operation.startswith("editor_") else {2}):
                raise ReceiptConflict("stale_contract_receipt")
            return stored[1], deepcopy(stored[2])

    def claim(self, operation: str, key: str, payload_digest: str) -> bool:
        with self._lock:
            stored = self._receipts.get((operation, key))
            if stored is not None:
                if stored[0] != payload_digest:
                    raise ReceiptConflict("idempotency_digest_conflict")
                return False
            self._receipts[(operation, key)] = (payload_digest, None, None)
            return True

    def store(self, operation: str, key: str, payload_digest: str, status: int, response: dict):
        if response.get("contract_version") not in ({1, 2} if operation.startswith("editor_") else {2}):
            raise ReceiptConflict("stale_contract_receipt")
        with self._lock:
            current = self._receipts.get((operation, key))
            candidate = (payload_digest, status, deepcopy(response))
            if current is not None and current != (payload_digest, None, None) and current != candidate:
                raise ReceiptConflict("idempotency_digest_conflict")
            self._receipts[(operation, key)] = candidate

    def release(self, operation: str, key: str, payload_digest: str) -> None:
        with self._lock:
            current = self._receipts.get((operation, key))
            if current is None:
                return
            if current[0] != payload_digest:
                raise ReceiptConflict("idempotency_digest_conflict")
            if current == (payload_digest, None, None):
                self._receipts.pop((operation, key))

    def recover_pending(self) -> tuple[tuple[str, str, str], ...]:
        with self._lock:
            pending = tuple(
                (operation, key, value[0])
                for (operation, key), value in sorted(self._receipts.items())
                if value[1] is None
            )
            for operation, key, _ in pending:
                self._receipts.pop((operation, key))
            return pending


class PostgresHTTPRepository:
    """Durable exact-replay receipts for mutating browser operations."""

    def __init__(self, connect) -> None:
        self._connect = connect

    def replay(self, operation: str, key: str, payload_digest: str):
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT payload_digest,http_status,response_body FROM hosted_http_operation_receipt WHERE operation=%s AND idempotency_key=%s",
                (operation, key),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        if row[0] != payload_digest:
            raise ReceiptConflict("idempotency_digest_conflict")
        if row[1] is None:
            return None
        body = json.loads(row[2]) if isinstance(row[2], str) else row[2]
        allowed_versions = {1, 2} if operation.startswith("editor_") else {2}
        if not isinstance(body, dict) or body.get("contract_version") not in allowed_versions:
            raise ReceiptConflict("stale_contract_receipt")
        return row[1], body

    def claim(self, operation: str, key: str, payload_digest: str) -> bool:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO hosted_http_operation_receipt(operation,idempotency_key,payload_digest,state) VALUES(%s,%s,%s,'pending') ON CONFLICT(operation,idempotency_key) DO NOTHING",
                (operation, key, payload_digest),
            )
            if cursor.rowcount:
                return True
            cursor.execute(
                "SELECT payload_digest FROM hosted_http_operation_receipt WHERE operation=%s AND idempotency_key=%s",
                (operation, key),
            )
            row = cursor.fetchone()
        if row is None or row[0] != payload_digest:
            raise ReceiptConflict("idempotency_digest_conflict")
        return False

    def store(self, operation: str, key: str, payload_digest: str, status: int, response: dict):
        allowed_versions = {1, 2} if operation.startswith("editor_") else {2}
        if response.get("contract_version") not in allowed_versions:
            raise ReceiptConflict("stale_contract_receipt")
        encoded = json.dumps(response, separators=(",", ":"), sort_keys=True)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO hosted_http_operation_receipt(operation,idempotency_key,payload_digest,state,http_status,response_body,completed_at) VALUES(%s,%s,%s,'completed',%s,%s::jsonb,now()) ON CONFLICT(operation,idempotency_key) DO NOTHING",
                (operation, key, payload_digest, status, encoded),
            )
            if cursor.rowcount:
                return
            cursor.execute(
                "UPDATE hosted_http_operation_receipt SET state='completed',http_status=%s,response_body=%s::jsonb,completed_at=now() WHERE operation=%s AND idempotency_key=%s AND payload_digest=%s AND state='pending'",
                (status, encoded, operation, key, payload_digest),
            )
            if cursor.rowcount:
                return
            cursor.execute(
                "SELECT payload_digest,http_status,response_body FROM hosted_http_operation_receipt WHERE operation=%s AND idempotency_key=%s AND state='completed'",
                (operation, key),
            )
            row = cursor.fetchone()
        if row is None:
            raise ReceiptConflict("idempotency_digest_conflict")
        stored_body = json.loads(row[2]) if isinstance(row[2], str) else row[2]
        if row[0] != payload_digest or row[1] != status or stored_body != response:
            raise ReceiptConflict("idempotency_digest_conflict")

    def release(self, operation: str, key: str, payload_digest: str) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM hosted_http_operation_receipt WHERE operation=%s AND idempotency_key=%s AND payload_digest=%s AND state='pending'",
                (operation, key, payload_digest),
            )
            if cursor.rowcount:
                return
            cursor.execute(
                "SELECT payload_digest FROM hosted_http_operation_receipt WHERE operation=%s AND idempotency_key=%s",
                (operation, key),
            )
            row = cursor.fetchone()
        if row is not None and row[0] != payload_digest:
            raise ReceiptConflict("idempotency_digest_conflict")

    def recover_pending(self) -> tuple[tuple[str, str, str], ...]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM hosted_http_operation_receipt WHERE state='pending' RETURNING operation,idempotency_key,payload_digest"
            )
            return tuple(cursor.fetchall())
