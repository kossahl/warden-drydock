"""Structured record-editor helpers.

The browser editor deals in typed records.  This module is the only translation
between that wire shape and the Markdown accepted by the deterministic engine.
It deliberately does not accept paths, Markdown patches, or database values.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping

from warden_drydock.hosted.engine.models import ChangeKind, ExactTextChange
from warden_drydock.standalone import frontmatter, parse_connections
from .contracts import canonical_digest, normalize_text, text_digest


_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_PUBLIC = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_STATUSES = {"idea", "draft", "review", "canon", "revealed", "archived", "accepted"}


def _id(value: Any, *, public: bool = False) -> str:
    pattern = _PUBLIC if public else _ID
    if not isinstance(value, str) or not 1 <= len(value) <= 80 or pattern.fullmatch(value) is None:
        raise ValueError("unsafe_identifier")
    return value


def authority_for(status: str) -> str:
    if status not in _STATUSES:
        raise ValueError("invalid_status")
    return status if status in {"canon", "revealed"} else "preparation"


def _unique(items: list[Mapping[str, Any]], key: str) -> None:
    values = [item.get(key) for item in items]
    if any(not isinstance(value, str) for value in values) or len(values) != len(set(values)):
        raise ValueError("duplicate_record_member_id")


def _visibility(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"audience", "warden_only"}:
        raise ValueError("invalid_visibility")
    audience, warden_only = value["audience"], value["warden_only"]
    if audience == "warden" and warden_only is True:
        return {"audience": audience, "warden_only": True}
    if audience in {"players", "shared"} and warden_only is False:
        return {"audience": audience, "warden_only": False}
    raise ValueError("invalid_visibility")


def _document(value: Mapping[str, Any]) -> dict[str, Any]:
    required = {"record_id", "record_type", "displayed_name", "status", "authority", "visibility", "fields", "sections", "connections", "content_digest"}
    if set(value) != required:
        raise ValueError("invalid_record_document")
    record_id = _id(value["record_id"])
    record_type = _id(value["record_type"])
    status = value["status"]
    if not isinstance(value["displayed_name"], str) or not 1 <= len(value["displayed_name"]) <= 200:
        raise ValueError("invalid_record_name")
    if value["authority"] != authority_for(status):
        raise ValueError("authority_status_mismatch")
    fields = list(value["fields"]); sections = list(value["sections"]); connections = list(value["connections"])
    if any(not isinstance(item, Mapping) or set(item) != {"field_id", "value"} for item in fields): raise ValueError("invalid_fields")
    if any(not isinstance(item, Mapping) or set(item) != {"section_id", "body"} for item in sections): raise ValueError("invalid_sections")
    if any(not isinstance(item, Mapping) or set(item) != {"connection_id", "target_record_id", "relationship", "state", "context"} for item in connections): raise ValueError("invalid_connections")
    _unique(fields, "field_id"); _unique(sections, "section_id"); _unique(connections, "connection_id")
    for item in fields: _id(item["field_id"])
    for item in sections:
        _id(item["section_id"])
        if not isinstance(item["body"], str) or len(item["body"]) > 200000: raise ValueError("invalid_section_body")
    for item in connections:
        for key in ("connection_id",): _id(item[key], public=True)
        for key in ("target_record_id", "relationship", "state"): _id(item[key])
        if not isinstance(item["context"], str) or not item["context"] or len(item["context"]) > 2000: raise ValueError("invalid_connection_context")
    if not isinstance(value["content_digest"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["content_digest"]): raise ValueError("invalid_content_digest")
    normalized = dict(value, fields=fields, sections=sections, connections=connections,
                      visibility=_visibility(value["visibility"]), authority=authority_for(status))
    if document_digest(normalized) != value["content_digest"]:
        raise ValueError("content_digest_mismatch")
    return normalized


def document_digest(value: Mapping[str, Any]) -> str:
    """Digest the typed document, excluding its self-referential digest."""
    return hashlib.sha256(json.dumps(
        {key: value[key] for key in (
            "record_id", "record_type", "displayed_name", "status", "authority",
            "visibility", "fields", "sections", "connections",
        )}, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()


def parse_document(content: str, record_id: str, record_type: str | None = None) -> dict[str, Any]:
    metadata = frontmatter(content)
    status = metadata.get("status", "draft")
    body = content
    if content.startswith("---\n"):
        end = content.find("\n---", 4)
        if end >= 0: body = content[end + 4:].lstrip("\n")
    sections: list[dict[str, str]] = []
    current = None
    in_connections = False
    for line in body.splitlines():
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match:
            heading = match.group(1).strip()
            in_connections = heading.casefold() == "connections"
            if in_connections:
                current = None
                continue
            current = {"section_id": re.sub(r"[^a-z0-9-]+", "-", heading.lower()).strip("-") or "summary", "body": ""}
            sections.append(current)
        elif current is not None and not in_connections:
            current["body"] += ("\n" if current["body"] else "") + line
    if not sections: sections = [{"section_id": "summary", "body": body.strip()}]
    connections, _ = parse_connections(content, source_id=record_id, path=None)  # type: ignore[arg-type]
    conn = [{"connection_id": f"connection_{index}", "target_record_id": item.target_id, "relationship": item.relationship, "state": item.state, "context": item.context} for index, item in enumerate(connections, 1)]
    fields = [{"field_id": key, "value": value} for key, value in metadata.items() if key not in {"id", "type", "name", "status", "visibility", "warden_only"}]
    audience = metadata.get("visibility", "warden")
    raw_warden_only = metadata.get("warden_only")
    warden_only = (raw_warden_only.lower() == "true") if isinstance(raw_warden_only, str) else (raw_warden_only if isinstance(raw_warden_only, bool) else audience == "warden")
    visibility = {"audience": audience, "warden_only": warden_only}
    value = {"record_id": record_id, "record_type": record_type or metadata.get("type", "unknown"), "displayed_name": metadata.get("name", record_id), "status": status, "authority": authority_for(status), "visibility": visibility, "fields": fields, "sections": sections, "connections": conn, "content_digest": "0" * 64}
    value["content_digest"] = document_digest(value)
    return _document(value)


def serialize_document(value: Mapping[str, Any]) -> str:
    value = _document(value)
    lines = ["---", f"id: {value['record_id']}", f"type: {value['record_type']}", f"name: {value['displayed_name']}", f"status: {value['status']}", f"visibility: {value['visibility']['audience']}", f"warden_only: {str(value['visibility']['warden_only']).lower()}"]
    for field in value["fields"]:
        if field["field_id"] == "warden_only":
            continue
        scalar = field["value"]
        lines.append(f"{field['field_id']}: {json.dumps(scalar, ensure_ascii=False) if not isinstance(scalar, str) else scalar}")
    lines += ["---", ""]
    for section in value["sections"]:
        lines += [f"## {section['section_id']}", section["body"], ""]
    if value["connections"]:
        lines += ["## Connections", ""]
        for item in value["connections"]:
            lines.append(f"- `{item['relationship']}` -> [[{item['target_record_id']}]] (`{item['state']}`) — {item['context']}")
    return normalize_text("\n".join(lines)).rstrip("\n") + "\n"


def _heading_id(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-") or "summary"


def _format_frontmatter_value(value: Any) -> str:
    if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_.:/+@ -]+", value or ""):
        return value
    return json.dumps(value, ensure_ascii=False)


def _connection_line(connection: Mapping[str, Any]) -> str:
    return (
        f"- `{connection['relationship']}` -> [[{connection['target_record_id']}]] "
        f"(`{connection['state']}`) — {connection['context']}"
    )


def mutate_document(before: str, candidate: Mapping[str, Any]) -> str:
    """Apply a typed candidate while retaining the source document's layout.

    The editor never treats Markdown as an input patch. It uses the parsed
    candidate to replace only known frontmatter, section, and typed-connection
    values. Comments, heading spelling, section order, and unrelated bytes stay
    in place. A no-op returns the original bytes exactly.
    """
    old = parse_document(before, candidate["record_id"], candidate.get("record_type"))
    new = _document(candidate)
    duplicate_connections = len(re.findall(r"(?im)^##\s+connections\s*$", normalize_text(before))) > 1
    if old == new and not duplicate_connections:
        return before
    newline = "\r\n" if "\r\n" in before else "\n"
    source = before.replace("\r\n", "\n").replace("\r", "\n")
    lines = source.splitlines(keepends=True)
    if not lines or not source.startswith("---\n"):
        return serialize_document(new).replace("\n", newline)
    end = next((index for index, line in enumerate(lines[1:], 1) if line.rstrip("\n") == "---"), None)
    if end is None:
        return serialize_document(new).replace("\n", newline)

    metadata_keys = {
        "id": new["record_id"], "type": new["record_type"],
        "name": new["displayed_name"], "status": new["status"],
        "visibility": new["visibility"]["audience"],
        "warden_only": new["visibility"]["warden_only"],
    }
    field_values = {item["field_id"]: item["value"] for item in new["fields"] if item["field_id"] != "warden_only"}
    all_values = {**metadata_keys, **field_values}
    old_field_values = {item["field_id"]: item["value"] for item in old["fields"]}
    original_keys = set()
    for index in range(1, end):
        match = re.match(r"^([^:#\s][^:]*):\s*(.*?)\s*\n?$", lines[index])
        if not match:
            continue
        key = match.group(1).strip()
        original_keys.add(key)
        changed = (
            (key in metadata_keys and {
                "id": old["record_id"], "type": old["record_type"],
                "name": old["displayed_name"], "status": old["status"],
                "visibility": old["visibility"]["audience"],
                "warden_only": old["visibility"]["warden_only"],
            }.get(key) != all_values[key])
            or (key in field_values and old_field_values.get(key) != field_values[key])
        )
        if key in all_values and changed:
            lines[index] = f"{key}: {_format_frontmatter_value(all_values[key])}{newline}"
    insert_at = end
    for key, value in all_values.items():
        if key not in original_keys:
            lines.insert(insert_at, f"{key}: {_format_frontmatter_value(value)}{newline}")
            insert_at += 1
    removed_keys = original_keys - set(all_values)
    if removed_keys:
        lines[1:end] = [line for line in lines[1:end] if not (
            (match := re.match(r"^([^:#\s][^:]*):", line)) and match.group(1).strip() in removed_keys
        )]
        end = next(index for index, line in enumerate(lines[1:], 1) if line.rstrip("\n") == "---")

    body_start = end + 1
    headings: list[tuple[int, str]] = []
    for index in range(body_start, len(lines)):
        match = re.match(r"^##\s+(.+?)\s*\n?$", lines[index])
        if match:
            headings.append((index, match.group(1).strip()))

    sections = {item["section_id"]: item["body"] for item in new["sections"]}
    consumed: set[str] = set()
    for position, (heading_index, heading) in enumerate(headings):
        if heading.casefold() == "connections":
            continue
        section_id = _heading_id(heading)
        if section_id not in sections:
            continue
        next_index = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        body_text = sections[section_id]
        old_body = next((item["body"] for item in old["sections"] if item["section_id"] == section_id), None)
        if old_body == body_text:
            consumed.add(section_id)
            continue
        replacement = [] if body_text == "" else body_text.splitlines(keepends=True)
        if replacement and not replacement[-1].endswith("\n"):
            replacement[-1] += "\n"
        lines[heading_index + 1:next_index] = replacement
        consumed.add(section_id)
        shift = len(replacement) - (next_index - heading_index - 1)
        headings = [(i if i <= heading_index else i + shift, h) for i, h in headings]

    # A typed candidate may intentionally remove a section.  Remove only the
    # matching heading block; headings, comments, and bytes in retained blocks
    # are otherwise left untouched.
    current_headings = []
    for index, line in enumerate(lines):
        match = re.match(r"^##\s+(.+?)\s*\n?$", line)
        if match:
            current_headings.append((index, match.group(1).strip()))
    for position, (heading_index, heading) in reversed(list(enumerate(current_headings))):
        if heading.casefold() == "connections" or _heading_id(heading) in sections:
            continue
        next_index = current_headings[position + 1][0] if position + 1 < len(current_headings) else len(lines)
        del lines[heading_index:next_index]

    # New sections are inserted before the typed Connections section, or at EOF.
    missing = [item for item in new["sections"] if item["section_id"] not in consumed]
    if missing:
        connection_index = next((i for i, line in enumerate(lines) if line.strip().casefold() == "## connections"), len(lines))
        inserted: list[str] = []
        for item in missing:
            inserted.extend([f"## {item['section_id']}{newline}"])
            if item["body"]:
                inserted.extend(item["body"].replace("\r\n", "\n").splitlines(keepends=True))
                if not inserted[-1].endswith("\n"):
                    inserted[-1] += newline
            inserted.append(newline)
        lines[connection_index:connection_index] = inserted

    connection_headers = [i for i, line in enumerate(lines) if line.strip().casefold() == "## connections"]
    # A source document may have acquired duplicate typed connection headings
    # outside the editor.  Keep the first section and its comments, while
    # folding all typed lines into the one canonical section below.
    for duplicate_index in reversed(connection_headers[1:]):
        del lines[duplicate_index]
    connection_index = next((i for i, line in enumerate(lines) if line.strip().casefold() == "## connections"), None)
    if connection_index is not None:
        if old["connections"] != new["connections"]:
            next_heading = next((i for i in range(connection_index + 1, len(lines)) if re.match(r"^##\s+", lines[i])), len(lines))
            kept = [line for line in lines[connection_index + 1:next_heading] if not line.lstrip().startswith("-")]
            connection_lines = [f"{_connection_line(item)}{newline}" for item in new["connections"]]
            lines[connection_index + 1:next_heading] = kept[:1] + connection_lines + kept[1:]
    elif new["connections"]:
        if lines and lines[-1].strip():
            lines.append(newline)
        lines.extend([f"## Connections{newline}", newline])
        lines.extend(f"{_connection_line(item)}{newline}" for item in new["connections"])
    result = "".join(lines)
    return result.replace("\n", newline) if newline != "\n" else result


def change_for(before: str | None, candidate: Mapping[str, Any], change_id: str, kind: ChangeKind) -> ExactTextChange:
    value = _document(candidate)
    replacement = "" if kind is ChangeKind.DELETE else (
        mutate_document(before, value) if before is not None else serialize_document(value)
    )
    return ExactTextChange(change_id, value["record_id"], text_digest(before) if before is not None else None, replacement, kind, value["record_type"])


def diff_digest(changes: tuple[ExactTextChange, ...]) -> str:
    return canonical_digest([{"change_id": c.change_id, "subject_id": c.subject_id, "change_type": c.change_kind.value, "before_digest": c.expected_content_digest, "after_digest": text_digest(c.replacement), "record_type": c.record_type} for c in changes])


@dataclass(frozen=True)
class EditorDraft:
    changes: tuple[ExactTextChange, ...]
    diff_digest: str
