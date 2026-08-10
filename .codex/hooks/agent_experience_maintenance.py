"""Read-only health audit for the local agent-experience queue."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import stat
from typing import Any


MAX_V2_BYTES = 16_384
STATUS_SCHEMA_VERSION = 1
LEGACY_FIELDS = (
    "captured_at", "session_id", "turn_id", "hook_event_name",
    "transcript_path", "cwd", "model", "permission_mode",
)
V2_FIELDS = (
    "event_id", *LEGACY_FIELDS, "agent", "skills", "outcome", "test_status",
    "correction_count", "input_tokens", "output_tokens", "files_changed_count",
    "field_provenance",
)
EVENT_CATEGORIES = (
    "valid_v1", "valid_v2", "malformed_json", "non_object", "unknown_schema",
    "identity_mismatch", "oversized_v2",
)
AGENTS = frozenset({
    "adapter_specialist", "agent_curator", "architect", "core_implementer",
    "docs_maintainer", "product_strategist", "reviewer", "test_engineer",
})
SKILLS = frozenset({"improve-drydock-agents", "plan-drydock-change", "verify-drydock-change"})
OUTCOMES = frozenset({"completed", "failed", "cancelled", "interrupted"})
TEST_STATUSES = frozenset({"passed", "failed", "partial", "not_run"})
COUNT_FIELDS = ("correction_count", "input_tokens", "output_tokens", "files_changed_count")
MAX_COUNT = 1_000_000_000_000
MAX_ID_LENGTH = 256
MAX_PATH_LENGTH = 4_096
MAX_LABEL_LENGTH = 256
MAX_IDENTIFIER_LENGTH = 64
RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


def _scalar_strings(event: dict[str, Any], fields: tuple[str, ...]) -> bool:
    return all(field in event and (event[field] is None or isinstance(event[field], str)) for field in fields)


def _valid_v2_optional(event: dict[str, Any]) -> bool:
    if event["agent"] is not None and (not isinstance(event["agent"], str) or event["agent"] not in AGENTS):
        return False
    if event["outcome"] is not None and (not isinstance(event["outcome"], str) or event["outcome"] not in OUTCOMES):
        return False
    if event["test_status"] is not None and (
        not isinstance(event["test_status"], str) or event["test_status"] not in TEST_STATUSES
    ):
        return False
    skills = event["skills"]
    if not isinstance(skills, list) or len(skills) > 16 or any(
        not isinstance(item, str) or item not in SKILLS for item in skills
    ):
        return False
    for field in COUNT_FIELDS:
        value = event[field]
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_COUNT):
            return False
    provenance = event["field_provenance"]
    if not isinstance(provenance, dict):
        return False
    expected_provenance = {
        key: "hook_payload." + key
        for key in ("agent", "skills", "outcome", "test_status", *COUNT_FIELDS)
        if event[key] not in (None, [])
    }
    return provenance == expected_provenance


def _bounded_nullable_string(value: object, maximum: int, *, nonempty: bool = False) -> bool:
    if value is None:
        return True
    return (
        isinstance(value, str)
        and len(value) <= maximum
        and (not nonempty or bool(value.strip()))
    )


def _valid_v2_legacy(event: dict[str, Any]) -> bool:
    return (
        _valid_timestamp(event["captured_at"])
        and _bounded_nullable_string(event["session_id"], MAX_ID_LENGTH, nonempty=True)
        and _bounded_nullable_string(event["turn_id"], MAX_ID_LENGTH, nonempty=True)
        and _bounded_nullable_string(event["hook_event_name"], MAX_IDENTIFIER_LENGTH)
        and _bounded_nullable_string(event["transcript_path"], MAX_PATH_LENGTH)
        and _bounded_nullable_string(event["cwd"], MAX_PATH_LENGTH)
        and _bounded_nullable_string(event["model"], MAX_LABEL_LENGTH)
        and _bounded_nullable_string(event["permission_mode"], MAX_IDENTIFIER_LENGTH)
    )


def _valid_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not RFC3339.fullmatch(value):
        return False
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.tzinfo is not None
    except ValueError:
        return False


def _classify(path: Path, raw: bytes) -> tuple[str, dict[str, Any] | None]:
    try:
        event = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "malformed_json", None
    if not isinstance(event, dict):
        return "non_object", None
    version = event.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool) or version not in (1, 2):
        return "unknown_schema", event
    if version == 2 and len(raw) > MAX_V2_BYTES:
        return "oversized_v2", event
    if version == 1:
        return ("valid_v1", event) if _scalar_strings(event, LEGACY_FIELDS) else ("unknown_schema", event)
    if event.get("event_id") != path.stem:
        return "identity_mismatch", event
    if any(field not in event for field in V2_FIELDS):
        return "unknown_schema", event
    if (
        not isinstance(event["event_id"], str)
        or not _valid_v2_legacy(event)
        or not _valid_v2_optional(event)
    ):
        return "unknown_schema", event
    return "valid_v2", event


def _load_processed(path: Path) -> tuple[set[str], str | None]:
    if not path.exists():
        return set(), None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return set(), type(exc).__name__
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("schema_version"), int)
        or isinstance(data.get("schema_version"), bool)
        or data.get("schema_version") != STATUS_SCHEMA_VERSION
    ):
        return set(), "invalid_schema"
    entries = data.get("events")
    if not isinstance(entries, dict):
        return set(), "invalid_schema"
    processed: set[str] = set()
    for filename, record in entries.items():
        if (
            not isinstance(filename, str)
            or Path(filename).name != filename
            or not filename.endswith(".json")
            or not isinstance(record, dict)
            or record.get("status") != "processed"
            or not _valid_timestamp(record.get("processed_at"))
        ):
            return set(), "invalid_schema"
        processed.add(filename)
    return processed, None


def audit_queue(
    queue_root: Path,
    processed_path: Path | None = None,
    *,
    older_than_days: float | None = None,
    larger_than_bytes: int | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Audit queue metadata without opening transcripts or mutating any path."""
    queue_root = queue_root.resolve()
    processed_path = (processed_path or queue_root.parent / "processed.json").resolve()
    processed_names, status_error = _load_processed(processed_path)
    categories: dict[str, list[str]] = {name: [] for name in EVENT_CATEGORIES}
    ignored: list[str] = []
    missing_transcript: list[str] = []
    processed: list[str] = []
    unprocessed: list[str] = []
    retention_age: list[str] = []
    retention_size: list[str] = []
    current = now or dt.datetime.now(dt.timezone.utc)
    current_ts = current.timestamp()

    if not queue_root.is_dir():
        entries: list[os.DirEntry[str]] = []
        queue_error = "queue_not_found"
    else:
        entries = sorted(os.scandir(queue_root), key=lambda entry: entry.name)
        queue_error = None

    for entry in entries:
        try:
            file_stat = entry.stat(follow_symlinks=False)
            regular = stat.S_ISREG(file_stat.st_mode)
        except OSError:
            regular = False
        if not regular or not entry.name.endswith(".json"):
            ignored.append(entry.name)
            continue
        path = queue_root / entry.name
        try:
            raw = path.read_bytes()
        except OSError:
            categories["malformed_json"].append(entry.name)
            continue
        category, event = _classify(path, raw)
        categories[category].append(entry.name)
        if category in ("valid_v1", "valid_v2"):
            (processed if entry.name in processed_names else unprocessed).append(entry.name)
            transcript = event.get("transcript_path") if event else None
            if isinstance(transcript, str) and transcript and not Path(transcript).exists():
                missing_transcript.append(entry.name)
        if older_than_days is not None:
            age_days = max(0.0, (current_ts - file_stat.st_mtime) / 86_400)
            if age_days >= older_than_days:
                retention_age.append(entry.name)
        if larger_than_bytes is not None and len(raw) >= larger_than_bytes:
            retention_size.append(entry.name)

    counts = {name: len(values) for name, values in categories.items()}
    counts.update({
        "ignored": len(ignored), "processed": len(processed), "unprocessed": len(unprocessed),
        "missing_transcript": len(missing_transcript),
        "retention_age_candidates": len(retention_age),
        "retention_size_candidates": len(retention_size),
    })
    return {
        "report_schema_version": 1,
        "queue_error": queue_error,
        "processed_state_error": status_error,
        "counts": counts,
        "categories": {**categories, "ignored": ignored},
        "processing": {"processed": processed, "unprocessed": unprocessed},
        "missing_transcript": missing_transcript,
        "retention_candidates": {"age": retention_age, "size": retention_size},
    }


def _parser() -> argparse.ArgumentParser:
    project_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="Audit the agent-experience queue without modifying it.")
    parser.add_argument("--queue-root", type=Path, default=project_root / ".agent-experience" / "pending")
    parser.add_argument("--processed-state", type=Path)
    parser.add_argument("--older-than-days", type=float, help="Report files at least this old; never delete them.")
    parser.add_argument("--larger-than-bytes", type=int, help="Report files at least this large; never delete them.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.older_than_days is not None and args.older_than_days < 0:
        raise SystemExit("--older-than-days must be non-negative")
    if args.larger_than_bytes is not None and args.larger_than_bytes < 0:
        raise SystemExit("--larger-than-bytes must be non-negative")
    report = audit_queue(
        args.queue_root, args.processed_state,
        older_than_days=args.older_than_days, larger_than_bytes=args.larger_than_bytes,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
