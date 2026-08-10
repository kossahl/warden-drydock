"""Queue a minimal Codex turn envelope for later agent-learning analysis."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import re
import sys


MAX_INPUT_BYTES = 65_536
MAX_EVENT_BYTES = 16_384
MAX_ID_LENGTH = 256
MAX_FILENAME_ID_LENGTH = 120
MAX_PATH_LENGTH = 4_096
MAX_LABEL_LENGTH = 256
MAX_IDENTIFIER_LENGTH = 64
MAX_SKILLS = 16
MAX_COUNT = 1_000_000_000_000
SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
AGENTS = frozenset({
    "adapter_specialist", "agent_curator", "architect", "core_implementer",
    "docs_maintainer", "product_strategist", "reviewer", "test_engineer",
})
SKILLS = frozenset({"improve-drydock-agents", "plan-drydock-change", "verify-drydock-change"})
OUTCOMES = frozenset({"completed", "failed", "cancelled", "interrupted"})
TEST_STATUSES = frozenset({"passed", "failed", "partial", "not_run"})
COUNT_FIELDS = ("correction_count", "input_tokens", "output_tokens", "files_changed_count")


def _bounded_string(value: object, maximum: int, *, nonempty: bool = False) -> str | None:
    if not isinstance(value, str) or len(value) > maximum or (nonempty and not value.strip()):
        return None
    return value


def _identifier(value: object) -> str | None:
    value = _bounded_string(value, MAX_IDENTIFIER_LENGTH, nonempty=True)
    return value if value is not None and IDENTIFIER.fullmatch(value) else None


def _allowlisted(value: object, accepted: frozenset[str]) -> str | None:
    value = _identifier(value)
    return value if value in accepted else None


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_COUNT else None


def _enumeration(value: object, accepted: frozenset[str]) -> str | None:
    return value if isinstance(value, str) and value in accepted else None


def _skills(value: object) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_SKILLS:
        return []
    result = [_allowlisted(item, SKILLS) for item in value]
    return [item for item in result if item is not None] if all(item is not None for item in result) else []


def _safe_id(value: str | None, fallback: str) -> str:
    cleaned = SAFE_ID.sub("-", value or "").strip("-.")
    return cleaned[:MAX_FILENAME_ID_LENGTH] or fallback


def _matching_existing(path: Path, event: dict[str, object]) -> bool:
    if not path.is_file():
        return False
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(existing, dict) or existing.get("schema_version") not in (1, 2):
        return False
    legacy = ("session_id", "turn_id", "hook_event_name", "transcript_path", "cwd", "model", "permission_mode")
    if not isinstance(existing.get("captured_at"), str) or any(
        key not in existing or (existing[key] is not None and not isinstance(existing[key], str)) for key in legacy
    ):
        return False
    if existing["session_id"] != event["session_id"] or existing["turn_id"] != event["turn_id"]:
        return False
    return existing.get("schema_version") == 1 or existing.get("event_id") == event["event_id"]


def capture(payload: dict[str, object], project_root: Path) -> Path:
    session_value = _bounded_string(payload.get("session_id"), MAX_ID_LENGTH, nonempty=True)
    turn_value = _bounded_string(payload.get("turn_id"), MAX_ID_LENGTH, nonempty=True)
    event_id = _safe_id(session_value, "unknown-session") + "--" + _safe_id(turn_value, "unknown-turn")
    queue_dir = project_root / ".agent-experience" / "pending"
    queue_dir.mkdir(parents=True, exist_ok=True)
    destination = queue_dir / (event_id + ".json")

    optional = {
        "agent": _allowlisted(payload.get("agent"), AGENTS),
        "skills": _skills(payload.get("skills")),
        "outcome": _enumeration(payload.get("outcome"), OUTCOMES),
        "test_status": _enumeration(payload.get("test_status"), TEST_STATUSES),
        **{key: _count(payload.get(key)) for key in COUNT_FIELDS},
    }
    event = {
        "schema_version": 2,
        "event_id": event_id,
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "session_id": session_value,
        "turn_id": turn_value,
        "hook_event_name": _bounded_string(payload.get("hook_event_name"), MAX_IDENTIFIER_LENGTH),
        "transcript_path": _bounded_string(payload.get("transcript_path"), MAX_PATH_LENGTH),
        "cwd": _bounded_string(payload.get("cwd"), MAX_PATH_LENGTH),
        "model": _bounded_string(payload.get("model"), MAX_LABEL_LENGTH),
        "permission_mode": _bounded_string(payload.get("permission_mode"), MAX_IDENTIFIER_LENGTH),
        **optional,
    }
    provenance = {
        key: "hook_payload." + key
        for key, value in optional.items() if value not in (None, [])
    }
    event["field_provenance"] = provenance
    encoded = (json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > MAX_EVENT_BYTES:
        raise ValueError("agent experience event exceeds byte limit")

    try:
        with destination.open("xb") as handle:
            handle.write(encoded)
    except FileExistsError:
        if not _matching_existing(destination, event):
            raise ValueError("event identity collides with a non-matching existing target")
    return destination


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise ValueError("hook input exceeds byte limit")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("hook input must be a JSON object")
        project_root = Path(__file__).resolve().parents[2]
        capture(payload, project_root)
        return 0
    except Exception as exc:
        print("agent experience capture failed: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
