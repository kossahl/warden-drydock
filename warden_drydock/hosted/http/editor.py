"""Structured record-editor helpers.

The browser editor deals in typed records.  This module is the only translation
between that wire shape and the Markdown accepted by the deterministic engine.
It deliberately does not accept paths, Markdown patches, or database values.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from warden_drydock.core.generator import DATA
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping

from warden_drydock.hosted.engine.models import ChangeKind, ExactTextChange
from warden_drydock.standalone import frontmatter, parse_connections
from .contracts import canonical_digest, normalize_text, text_digest


_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_PUBLIC = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_FIELD_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")
_CONNECTION_MARKER = re.compile(
    r"^\s*<!--\s*drydock:connection-id=(?P<id>[a-z][a-z0-9]*(?:_[a-z0-9]+)*)\s*-->\s*$"
)
_STATUSES = {"idea", "draft", "review", "canon", "revealed", "archived", "accepted"}
_MAX_SAFE_INTEGER = 2**53 - 1
_CONNECTION_LINE_BOUNDARIES = frozenset("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")


def _contains_connection_line_boundary(value: str) -> bool:
    return any(character in _CONNECTION_LINE_BOUNDARIES for character in value)


def _split_lf_lines(value: str) -> list[str]:
    """Split only on LF, retaining LF endings for source-preserving edits."""
    parts = value.split("\n")
    lines = [f"{part}\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _id(value: Any, *, public: bool = False) -> str:
    pattern = _PUBLIC if public else _ID
    minimum = 3 if public else 1
    if not isinstance(value, str) or not minimum <= len(value) <= 80 or pattern.fullmatch(value) is None:
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


def _typed_equal(left: Any, right: Any) -> bool:
    """Compare JSON values without treating distinct numeric types as equal."""
    if type(left) is not type(right):
        return False
    if isinstance(left, Mapping):
        return set(left) == set(right) and all(_typed_equal(left[key], right[key]) for key in left)
    if isinstance(left, (list, tuple)):
        return len(left) == len(right) and all(_typed_equal(a, b) for a, b in zip(left, right))
    return left == right


def _document(value: Mapping[str, Any]) -> dict[str, Any]:
    required = {"record_id", "record_type", "displayed_name", "status", "authority", "visibility", "fields", "sections", "connections", "content_digest"}
    if set(value) != required:
        raise ValueError("invalid_record_document")
    record_id = _id(value["record_id"])
    record_type = _id(value["record_type"])
    status = value["status"]
    if (
        not isinstance(value["displayed_name"], str)
        or not 1 <= len(value["displayed_name"]) <= 200
        or not value["displayed_name"].strip()
        or "\n" in value["displayed_name"]
        or "\r" in value["displayed_name"]
    ):
        raise ValueError("invalid_record_name")
    if value["authority"] != authority_for(status):
        raise ValueError("authority_status_mismatch")
    fields = list(value["fields"]); raw_sections = list(value["sections"]); connections = list(value["connections"])
    if any(not isinstance(item, Mapping) or set(item) != {"field_id", "value"} for item in fields): raise ValueError("invalid_fields")
    if any(not isinstance(item, Mapping) or set(item) != {"section_id", "body"} for item in raw_sections): raise ValueError("invalid_sections")
    if any(not isinstance(item, Mapping) or set(item) != {"connection_id", "target_record_id", "relationship", "state", "context"} for item in connections): raise ValueError("invalid_connections")
    for item in fields:
        scalar = item["value"]
        if not (
            scalar is None
            or isinstance(scalar, (str, bool))
            or (
                isinstance(scalar, int)
                and not isinstance(scalar, bool)
                and abs(scalar) <= _MAX_SAFE_INTEGER
            )
            or (
                isinstance(scalar, float)
                and math.isfinite(scalar)
                and not scalar.is_integer()
            )
        ):
            raise ValueError("invalid_field_value")
    if any(not isinstance(item["body"], str) or len(item["body"]) > 200000 for item in raw_sections): raise ValueError("invalid_section_body")
    sections = [dict(item, body=normalize_text(item["body"])) for item in raw_sections]
    _unique(fields, "field_id"); _unique(sections, "section_id"); _unique(connections, "connection_id")
    for item in fields:
        if not isinstance(item["field_id"], str) or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", item["field_id"]) is None:
            raise ValueError("unsafe_identifier")
    for item in sections:
        _id(item["section_id"])
        if not isinstance(item["body"], str) or len(item["body"]) > 200000: raise ValueError("invalid_section_body")
    for item in connections:
        for key in ("connection_id",): _id(item[key], public=True)
        for key in ("target_record_id", "relationship", "state"): _id(item[key])
        if (
            not isinstance(item["context"], str)
            or not item["context"]
            or len(item["context"]) > 2000
            or _contains_connection_line_boundary(item["context"])
            or item["context"] != item["context"].strip()
        ):
            raise ValueError("invalid_connection_context")
    if not isinstance(value["content_digest"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["content_digest"]): raise ValueError("invalid_content_digest")
    normalized = dict(value, fields=fields, sections=sections, connections=connections,
                      visibility=_visibility(value["visibility"]), authority=authority_for(status))
    if document_digest(normalized) != value["content_digest"]:
        raise ValueError("content_digest_mismatch")
    return normalized


def document_digest(value: Mapping[str, Any]) -> str:
    """Digest the typed document, excluding its self-referential digest."""
    sections = [dict(item, body=normalize_text(item["body"])) for item in value["sections"]]
    return hashlib.sha256(json.dumps(
        {key: value[key] for key in (
            "record_id", "record_type", "displayed_name", "status", "authority",
            "visibility", "fields", "connections",
        )} | {"sections": sections}, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()


def _connection_markers(content: str) -> dict[int, str]:
    """Read editor-only occurrence IDs from the first typed Connections block."""
    return {
        line_number: marker_id
        for line_number, (_, marker_id) in _connection_marker_occurrences(content).items()
    }


def _connection_marker_occurrences(content: str) -> dict[int, tuple[int, str]]:
    """Map each typed connection line to its associated marker line and ID."""
    markers: dict[int, tuple[int, str]] = {}
    in_connections = False
    pending: tuple[int, str] | None = None
    for line_number, line in enumerate(content.split("\n"), 1):
        heading = re.match(r"^##\s+(.+?)\s*$", line)
        if heading:
            if in_connections:
                break
            in_connections = heading.group(1).strip().casefold() == "connections"
            pending = None
            continue
        if not in_connections:
            continue
        marker = _CONNECTION_MARKER.fullmatch(line)
        if marker:
            pending = (line_number, marker.group("id"))
            continue
        if line.lstrip().startswith("-"):
            if pending is not None:
                markers[line_number] = pending
            pending = None
        elif line.strip() and not line.lstrip().startswith("<!--"):
            pending = None
    return markers


def _heading_id(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-") or "summary"


def _unique_section_id(heading: str, used: set[str]) -> str:
    base = _heading_id(heading)
    section_id = base
    suffix = 2
    while section_id in used:
        section_id = f"{base}-{suffix}"
        suffix += 1
    used.add(section_id)
    return section_id


def _section_headings(headings: list[tuple[int, str]]) -> list[tuple[int, str, str | None]]:
    used: set[str] = set()
    return [
        (index, heading, None if heading.casefold() == "connections" else _unique_section_id(heading, used))
        for index, heading in headings
    ]


def parse_document(content: str, record_id: str, record_type: str | None = None) -> dict[str, Any]:
    normalized_content = content.replace("\r\n", "\n").replace("\r", "\n")
    metadata = frontmatter(normalized_content)
    status = metadata.get("status", "draft")
    body = normalized_content
    if normalized_content.startswith("---\n"):
        end = normalized_content.find("\n---", 4)
        if end >= 0: body = normalized_content[end + 4:].lstrip("\n")
    sections: list[dict[str, str]] = []
    current = None
    in_connections = False
    body_lines = body.split("\n")
    current_body: list[str] | None = None
    used_section_ids: set[str] = set()

    def finish_section(*, at_eof: bool = False) -> None:
        if current is None or current_body is None:
            return
        # At EOF the serializer adds one structural newline after the final
        # section. It must not become part of the typed body, while any
        # preceding empty lines remain intentional content.
        lines = current_body[:-1] if at_eof and current_body and current_body[-1] == "" else current_body
        current["body"] = "\n".join(lines)

    for line in body_lines:
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match:
            finish_section()
            heading = match.group(1).strip()
            in_connections = heading.casefold() == "connections"
            if in_connections:
                current = None
                current_body = None
                continue
            current = {"section_id": _unique_section_id(heading, used_section_ids), "body": ""}
            sections.append(current)
            current_body = []
        elif current is not None and not in_connections:
            assert current_body is not None
            current_body.append(line)
            current["body"] = "\n".join(current_body)
    finish_section(at_eof=True)
    if not sections: sections = [{"section_id": "summary", "body": body.strip()}]
    connection_markers = _connection_markers(normalized_content)
    connections, _ = parse_connections(normalized_content, source_id=record_id, path=None)  # type: ignore[arg-type]
    conn = []
    for index, item in enumerate(connections, 1):
        connection_id = connection_markers.get(item.line, f"connection_{index}")
        _id(connection_id, public=True)
        conn.append({"connection_id": connection_id, "target_record_id": item.target_id,
                     "relationship": item.relationship, "state": item.state,
                     "context": item.context.rstrip()})
    fields = [{"field_id": key, "value": value} for key, value in metadata.items()
              if key not in {"id", "type", "name", "status", "visibility", "warden_only"}
              and _FIELD_ID.fullmatch(key)]
    audience = metadata.get("visibility", "warden")
    raw_warden_only = metadata.get("warden_only")
    warden_only = (raw_warden_only.lower() == "true") if isinstance(raw_warden_only, str) else (raw_warden_only if isinstance(raw_warden_only, bool) else audience == "warden")
    visibility = {"audience": audience, "warden_only": warden_only}
    value = {"record_id": record_id, "record_type": record_type or metadata.get("type", "unknown"), "displayed_name": metadata.get("name", record_id), "status": status, "authority": authority_for(status), "visibility": visibility, "fields": fields, "sections": sections, "connections": conn, "content_digest": "0" * 64}
    value["content_digest"] = document_digest(value)
    return _document(value)


def serialize_document(value: Mapping[str, Any]) -> str:
    value = _document(value)
    lines = [
        "---",
        f"id: {_format_frontmatter_value(value['record_id'])}",
        f"type: {_format_frontmatter_value(value['record_type'])}",
        f"name: {_format_frontmatter_value(value['displayed_name'])}",
        f"status: {_format_frontmatter_value(value['status'])}",
        f"visibility: {_format_frontmatter_value(value['visibility']['audience'])}",
        f"warden_only: {_format_frontmatter_value(value['visibility']['warden_only'])}",
    ]
    for field in value["fields"]:
        if field["field_id"] == "warden_only":
            continue
        scalar = field["value"]
        lines.append(f"{field['field_id']}: {_format_frontmatter_value(scalar)}")
    lines += ["---", ""]
    for section in value["sections"]:
        lines += [f"## {section['section_id']}", normalize_text(section["body"])]
    if value["connections"]:
        lines += ["## Connections", ""]
        for item in value["connections"]:
            lines.append(f"<!-- drydock:connection-id={item['connection_id']} -->")
            lines.append(f"- `{item['relationship']}` -> [[{item['target_record_id']}]] (`{item['state']}`) — {item['context']}")
    return normalize_text("\n".join(lines)) + "\n"


def _format_frontmatter_value(value: Any) -> str:
    def encoded(item: Any) -> str:
        # Python's ensure_ascii=False leaves U+2028/U+2029 literal. Markdown
        # readers commonly treat those characters as line boundaries, so keep
        # them escaped inside frontmatter JSON strings.
        return json.dumps(item, ensure_ascii=False).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")

    if isinstance(value, str):
        if value == value.strip() and re.fullmatch(r"[a-zA-Z0-9_.:/+@ -]+", value or ""):
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError:
                return value
            if not (decoded is None or isinstance(decoded, bool) or (isinstance(decoded, (int, float)) and math.isfinite(decoded))):
                return value
        return encoded(value)
    return encoded(value)


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
    old_section_ids = [item["section_id"] for item in old["sections"]]
    new_section_ids = [item["section_id"] for item in new["sections"]]
    old_common = [section_id for section_id in old_section_ids if section_id in new_section_ids]
    new_common = [section_id for section_id in new_section_ids if section_id in old_section_ids]
    if old_common != new_common:
        raise ValueError("editor_section_reordering_not_allowed")
    if _typed_equal(old, new):
        return before
    newline = "\r\n" if "\r\n" in before else "\n"
    source = before.replace("\r\n", "\n").replace("\r", "\n")
    lines = _split_lf_lines(source)
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
            or (
                key in field_values
                and (
                    key not in old_field_values
                    or not _typed_equal(old_field_values[key], field_values[key])
                )
            )
        )
        if key in all_values and changed:
            lines[index] = f"{key}: {_format_frontmatter_value(all_values[key])}{newline}"
    insert_at = end
    for key, value in all_values.items():
        if key not in original_keys:
            lines.insert(insert_at, f"{key}: {_format_frontmatter_value(value)}{newline}")
            insert_at += 1
    managed_keys = set(metadata_keys) | set(old_field_values)
    removed_keys = (original_keys & managed_keys) - set(all_values)
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
    section_headings = _section_headings(headings)

    sections = {item["section_id"]: item["body"] for item in new["sections"]}
    consumed: set[str] = set()
    if not headings and "summary" in sections:
        # With no headings, parse_document exposes the whole body as a
        # synthetic summary. Keep that body unheaded instead of appending a
        # second copy under a new heading during metadata-only edits.
        old_body = next((item["body"] for item in old["sections"] if item["section_id"] == "summary"), None)
        body_text = normalize_text(sections["summary"])
        if old_body != body_text:
            content_start = body_start
            while content_start < len(lines) and not lines[content_start].strip():
                content_start += 1
            replacement = [] if body_text == "" else _split_lf_lines(body_text)
            if replacement and not replacement[-1].endswith("\n"):
                replacement[-1] += newline
            lines[content_start:] = replacement
        consumed.add("summary")
    # Replace from the end so offsets collected from the original source stay
    # valid while earlier sections are still waiting to be changed.
    for position, (heading_index, heading, section_id) in reversed(list(enumerate(section_headings))):
        if section_id is None:
            continue
        if section_id not in sections:
            continue
        next_index = section_headings[position + 1][0] if position + 1 < len(section_headings) else len(lines)
        body_text = normalize_text(sections[section_id])
        old_body = next((item["body"] for item in old["sections"] if item["section_id"] == section_id), None)
        if old_body == body_text:
            consumed.add(section_id)
            continue
        replacement = [] if body_text == "" else _split_lf_lines(body_text)
        if replacement and not replacement[-1].endswith("\n"):
            replacement[-1] += "\n"
        # ``parse_document`` represents the blank line before the next
        # heading as the body's trailing newline.  Keep that structural line
        # when the reviewed body itself ends with a newline, including at EOF.
        if replacement and body_text.endswith("\n"):
            replacement.append("\n")
        lines[heading_index + 1:next_index] = replacement
        consumed.add(section_id)

    # A typed candidate may intentionally remove a section.  Remove only the
    # matching heading block; headings, comments, and bytes in retained blocks
    # are otherwise left untouched.
    current_headings = []
    for index, line in enumerate(lines):
        match = re.match(r"^##\s+(.+?)\s*\n?$", line)
        if match:
            current_headings.append((index, match.group(1).strip()))
    current_section_headings = _section_headings(current_headings)
    for position, (heading_index, heading, section_id) in reversed(list(enumerate(current_section_headings))):
        if section_id is None or section_id in sections:
            continue
        next_index = current_section_headings[position + 1][0] if position + 1 < len(current_section_headings) else len(lines)
        del lines[heading_index:next_index]

    # New sections are inserted before the typed Connections section, or at EOF.
    missing = [item for item in new["sections"] if item["section_id"] not in consumed]
    if missing:
        connection_index = next((i for i, line in enumerate(lines) if line.strip().casefold() == "## connections"), len(lines))
        inserted: list[str] = []
        for item in missing:
            inserted.extend([f"## {item['section_id']}{newline}"])
            if item["body"]:
                inserted.extend(_split_lf_lines(normalize_text(item["body"])))
                if not inserted[-1].endswith("\n"):
                    inserted[-1] += newline
            inserted.append(newline)
        lines[connection_index:connection_index] = inserted

    connection_headers = [i for i, line in enumerate(lines) if line.strip().casefold() == "## connections"]

    def typed_connection_slots(heading_index: int, next_heading: int) -> list[tuple[int, int | None, str]]:
        """Return parser-identified rows and their associated editor markers."""
        segment = lines[heading_index + 1:next_heading]
        block = "## Connections\n" + "".join(segment)
        typed_connections, _ = parse_connections(
            block, source_id=new["record_id"], path=None  # type: ignore[arg-type]
        )
        occurrences = _connection_marker_occurrences(block)
        return [
            (
                heading_index + connection.line - 1,
                heading_index + occurrences[connection.line][0] - 1 if connection.line in occurrences else None,
                occurrences[connection.line][1] if connection.line in occurrences else f"connection_{index}",
            )
            for index, connection in enumerate(typed_connections, 1)
        ]

    def typed_connection_indexes(heading_index: int, next_heading: int) -> tuple[set[int], set[int]]:
        slots = typed_connection_slots(heading_index, next_heading)
        return {line for line, _, _ in slots}, {marker for _, marker, _ in slots if marker is not None}

    # Duplicate Connections headings are authored source boundaries. The
    # standalone parser reads only the first block, so preserve every later
    # block byte-for-byte instead of deleting rows that are outside the typed
    # candidate.
    connection_index = next((i for i, line in enumerate(lines) if line.strip().casefold() == "## connections"), None)
    if connection_index is not None:
        if old["connections"] != new["connections"]:
            next_heading = next((i for i in range(connection_index + 1, len(lines)) if re.match(r"^##\s+", lines[i])), len(lines))
            connection_lines = []
            for item in new["connections"]:
                connection_lines.extend([
                    f"<!-- drydock:connection-id={item['connection_id']} -->{newline}",
                    f"{_connection_line(item)}{newline}",
                ])
            # Use `parse_connections`' typed line numbers as the replacement set
            # instead of treating every Markdown bullet as editor data.
            slots = typed_connection_slots(connection_index, next_heading)
            typed_line_indexes = {line for line, _, _ in slots}
            marker_line_indexes = {marker for _, marker, _ in slots if marker is not None}
            slots_by_line = {line: connection_id for line, _, connection_id in slots}
            connections_by_id = {item["connection_id"]: item for item in new["connections"]}
            emitted_ids: set[str] = set()
            segment = lines[connection_index + 1:next_heading]
            if typed_line_indexes:
                rewritten: list[str] = []
                for index, line in enumerate(segment, connection_index + 1):
                    if index in marker_line_indexes:
                        continue
                    if index in typed_line_indexes:
                        connection_id = slots_by_line[index]
                        item = connections_by_id.get(connection_id)
                        if item is not None:
                            rewritten.extend([
                                f"<!-- drydock:connection-id={item['connection_id']} -->{newline}",
                                f"{_connection_line(item)}{newline}",
                            ])
                            emitted_ids.add(connection_id)
                        continue
                    rewritten.append(line)
                additions = [item for item in new["connections"] if item["connection_id"] not in emitted_ids]
                if additions:
                    added_lines = [line for item in additions for line in (
                        f"<!-- drydock:connection-id={item['connection_id']} -->{newline}",
                        f"{_connection_line(item)}{newline}",
                    )]
                    insert_at = len(rewritten)
                    while insert_at > 0 and not rewritten[insert_at - 1].strip():
                        insert_at -= 1
                    rewritten[insert_at:insert_at] = added_lines
                lines[connection_index + 1:next_heading] = rewritten
            else:
                lines[connection_index + 1:next_heading] = segment[:1] + connection_lines + segment[1:]
    elif new["connections"]:
        if lines and lines[-1].strip():
            lines.append(newline)
        lines.extend([f"## Connections{newline}", newline])
        for item in new["connections"]:
            lines.extend([
                f"<!-- drydock:connection-id={item['connection_id']} -->{newline}",
                f"{_connection_line(item)}{newline}",
            ])
    # Candidate text may arrive from a browser with CRLF already embedded in a
    # section body.  Normalize the assembled result before restoring the source
    # convention so CRLF never becomes CRCRLF.
    result = normalize_text("".join(lines))
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


@lru_cache(maxsize=None)
def adapter_editor_definition(adapter_id: str, revision_root: Path | None = None) -> dict:
    """Read the adapter definition bound to the revision being edited."""
    root = revision_root or DATA / "adapters" / _id(adapter_id)
    config = json.loads((root / "00-drydock/adapter.json").read_text(encoding="utf-8"))
    definitions = {}
    sources = [
        (kind, (root / spec["template"]).read_text(encoding="utf-8"), spec)
        for kind, spec in config["entity_types"].items()
    ]
    project_template_root = DATA / "project_template"
    for path in project_template_root.rglob("*.md"):
        bound_path = (revision_root / path.relative_to(project_template_root)) if revision_root else path
        if not bound_path.is_file():
            continue
        source = bound_path.read_text(encoding="utf-8")
        kind = frontmatter(source).get("type")
        if kind and kind not in config["entity_types"]:
            sources.append((kind, source, {}))
    for kind, source, rules in sources:
        metadata = frontmatter(source)
        section_order = []
        section_labels = {}
        for heading in re.findall(r"^## (.+)$", source, re.M):
            if heading.casefold() == "connections":
                continue
            section_id = _heading_id(heading)
            if section_id not in section_labels:
                section_order.append(section_id)
                section_labels[section_id] = heading
        definitions[kind] = {
            "metadata": metadata,
            "fields": set(metadata) - {"id", "type", "name", "status", "visibility", "warden_only"},
            "field_defaults": {
                field: metadata[field]
                for field in set(metadata) - {"id", "type", "name", "status", "visibility", "warden_only"}
            },
            "sections": set(section_order),
            "section_order": tuple(section_order),
            "section_labels": section_labels,
            "required_fields": set(rules.get("required_fields", [])),
            "nonempty_fields": set(rules.get("nonempty_fields", [])),
            "required_values": dict(rules.get("required_values", {})),
            "forbidden_headings": set(rules.get("forbidden_headings", [])),
        }
    return {"records": definitions, "creatable": set(config["entity_types"]),
            "relationships": set(config["connections"]["relationships"]),
            "states": set(config["connections"]["states"])}


def adapter_editor_contract(definition: dict) -> dict:
    """Return the JSON-safe definition used by the bound editor client."""
    return {
        "record_types": sorted(definition["creatable"]),
        "relationships": sorted(definition["relationships"]),
        "connection_states": sorted(definition["states"]),
        "record_definitions": {
            kind: {
                "metadata": spec["metadata"],
                "fields": sorted(spec["fields"]),
                "field_defaults": spec["field_defaults"],
                "sections": [
                    {"id": section_id, "label": spec["section_labels"][section_id]}
                    for section_id in spec["section_order"]
                ],
                "required_fields": sorted(spec["required_fields"]),
                "nonempty_fields": sorted(spec["nonempty_fields"]),
                "required_values": spec["required_values"],
                "forbidden_headings": sorted(spec["forbidden_headings"]),
            }
            for kind, spec in sorted(definition["records"].items())
        },
    }


def validate_adapter_document(candidate: dict, definition: dict, before: dict | None) -> None:
    spec = definition["records"].get(candidate["record_type"])
    if spec is None or (before is None and candidate["record_type"] not in definition["creatable"]):
        raise ValueError("record_type_unknown")
    if before is not None:
        old_section_ids = [item["section_id"] for item in before["sections"]]
        new_section_ids = [item["section_id"] for item in candidate["sections"]]
        old_common = [section_id for section_id in old_section_ids if section_id in new_section_ids]
        new_common = [section_id for section_id in new_section_ids if section_id in old_section_ids]
        if old_common != new_common:
            raise ValueError("editor_section_reordering_not_allowed")
    for collection, key in (("fields", "field_id"), ("sections", "section_id")):
        old = {item[key]: item for item in before[collection]} if before else {}
        new = {item[key]: item for item in candidate[collection]}
        for identifier in old.keys() | new.keys():
            if identifier not in spec[collection] and not _typed_equal(old.get(identifier), new.get(identifier)):
                raise ValueError("unsupported_editor_" + collection)
            if identifier in old and identifier not in new:
                raise ValueError("editor_member_removal_not_allowed")
        for identifier, item in new.items():
            if old.get(identifier) == item:
                continue
            text = item.get("body", item.get("value"))
            if isinstance(text, str) and (re.search(r"^##\s", text, re.M) if collection == "sections" else "\n" in text or "\r" in text):
                raise ValueError("invalid_editor_member_content")
    values = {
        "id": candidate["record_id"],
        "type": candidate["record_type"],
        "name": candidate["displayed_name"],
        "status": candidate["status"],
        "visibility": candidate["visibility"]["audience"],
        "warden_only": str(candidate["visibility"]["warden_only"]).lower(),
        **{item["field_id"]: item["value"] for item in candidate["fields"]},
    }
    missing = [field for field in spec["required_fields"] if field not in values]
    if missing:
        raise ValueError("missing_required_adapter_field")
    for field in spec["nonempty_fields"]:
        value = values.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError("empty_required_adapter_field")
    for field, required_value in spec["required_values"].items():
        if str(values.get(field, "")).lower() != str(required_value).lower():
            raise ValueError("adapter_required_value")
    forbidden = {_heading_id(heading) for heading in spec["forbidden_headings"]}
    if any(section["section_id"] in forbidden for section in candidate["sections"]):
        raise ValueError("forbidden_editor_heading")
    for connection in candidate["connections"]:
        if connection["relationship"] not in definition["relationships"]:
            raise ValueError("unsupported_connection_relationship")
        if connection["state"] not in definition["states"]:
            raise ValueError("unsupported_connection_state")
