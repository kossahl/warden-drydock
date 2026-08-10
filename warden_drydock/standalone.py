"""Portable campaign maintenance commands.

This module intentionally uses only the Python standard library. The generator
copies it verbatim into campaign repositories as ``scripts/drydock.py`` while
the framework imports the same implementation.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import re
from pathlib import Path


VALID_STATUSES = {
    "idea",
    "draft",
    "review",
    "canon",
    "revealed",
    "archived",
    "accepted",
}
APPROVED_SESSION_STATUSES = {"canon", "revealed", "accepted"}
VALID_OWNERSHIP = {"framework", "adapter", "shared", "campaign", "generated"}
REQUIRED_MANIFEST_FIELDS = {
    "framework": str,
    "framework_version": str,
    "adapter": str,
    "adapter_version": str,
    "ownership_model": int,
    "campaign_name": str,
}
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
HEADING_PATTERN = re.compile(r"(?m)^#{1,6}\s+(.+?)\s*$")
CONNECTION_PATTERN = re.compile(
    r"^\s*-\s+`(?P<relationship>[a-z0-9][a-z0-9-]*)`\s+"
    r"(?:→|->)\s+\[\[(?P<target>[a-z0-9][a-z0-9-]*)(?:\|[^\]]+)?\]\]\s+"
    r"\(`(?P<state>[a-z0-9][a-z0-9-]*)`\)\s+—\s+(?P<context>\S.*)\s*$"
)
VALID_CONNECTION_STATES = {
    "current", "former", "planned", "possible", "disputed", "believed",
    "hidden", "confirmed", "inactive", "unknown", "active",
}


@dataclass(frozen=True)
class Entity:
    entity_id: str
    entity_type: str
    name: str
    status: str
    visibility: str
    path: Path
    text: str


@dataclass(frozen=True)
class Connection:
    source_id: str
    target_id: str
    relationship: str
    state: str
    context: str
    path: Path
    line: int


def _section_lines(text: str, heading: str) -> list[tuple[int, str]]:
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip().casefold() == f"## {heading}".casefold():
            start = index + 1
            break
    if start is None:
        return []
    result = []
    for index in range(start, len(lines)):
        if lines[index].startswith("## "):
            break
        result.append((index + 1, lines[index]))
    return result


def parse_connections(text: str, *, source_id: str, path: Path) -> tuple[list[Connection], list[str]]:
    connections: list[Connection] = []
    errors: list[str] = []
    for line_number, line in _section_lines(text, "Connections"):
        if not line.strip() or line.lstrip().startswith("<!--"):
            continue
        if not line.lstrip().startswith("-"):
            continue
        match = CONNECTION_PATTERN.fullmatch(line)
        if not match:
            errors.append(f"{path}:{line_number}: malformed connection")
            continue
        connections.append(Connection(source_id=source_id, target_id=match['target'],
            relationship=match['relationship'], state=match['state'],
            context=match['context'], path=path, line=line_number))
    return connections, errors


def _entities(root: Path) -> dict[str, Entity]:
    result: dict[str, Entity] = {}
    for path in sorted(root.rglob("*.md")):
        relative = path.relative_to(root)
        if relative.parts[0] in {"templates", "docs"} or relative.as_posix().startswith("00-drydock/"):
            continue
        text = path.read_text(encoding="utf-8")
        metadata = frontmatter(text)
        entity_id = metadata.get("id")
        if entity_id:
            result[entity_id] = Entity(entity_id, metadata.get("type", "unknown"),
                metadata.get("name") or entity_id, metadata.get("status", "unknown"),
                metadata.get("visibility", "warden"), relative, text)
    return result


def _graph(root: Path) -> tuple[dict[str, Entity], list[Connection], list[str]]:
    entities = _entities(root)
    connections: list[Connection] = []
    errors: list[str] = []
    for entity in entities.values():
        parsed, failures = parse_connections(entity.text, source_id=entity.entity_id, path=entity.path)
        connections.extend(parsed)
        errors.extend(failures)
    return entities, connections, errors


def _graph_or_exit(root: Path) -> tuple[dict[str, Entity], list[Connection]]:
    entities, connections, errors = _graph(root)
    if errors:
        raise SystemExit("Cannot build relationship data:\n" + "\n".join(errors))
    return entities, connections


def frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end < 0:
        return {}
    result: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" in line and not line.startswith(" "):
            key, value = line.split(":", 1)
            result[key.strip()] = value.strip().strip('"')
    return result


def body(text: str) -> str:
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end >= 0:
            return text[end + 4 :].strip()
    return text.strip()


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _adapter_config(root: Path) -> dict:
    path = root / "00-drydock" / "adapter.json"
    if not path.exists():
        return {"entity_types": {}}
    return _read_json(path)


def validate_campaign(root: Path) -> int:
    root = root.resolve()
    errors: list[str] = []
    warnings: list[str] = []
    ids: dict[str, Path] = {}
    manifest = root / ".drydock.json"
    manifest_data: dict = {}
    if not manifest.exists():
        errors.append("Missing .drydock.json")
    else:
        try:
            manifest_data = _read_json(manifest)
            for field, expected_type in REQUIRED_MANIFEST_FIELDS.items():
                value = manifest_data.get(field)
                if not isinstance(value, expected_type) or value == "":
                    errors.append(f".drydock.json: invalid or missing {field}")
        except json.JSONDecodeError as exc:
            errors.append(f"Invalid .drydock.json: {exc}")

    lock_path = root / ".drydock-lock.json"
    lock: dict = {}
    if not lock_path.exists():
        errors.append("Missing .drydock-lock.json")
    else:
        try:
            lock = _read_json(lock_path)
            if lock.get("schema_version") != 1:
                errors.append(".drydock-lock.json: unsupported schema_version")
            lock_files = lock.get("files")
            if not isinstance(lock_files, dict):
                errors.append(".drydock-lock.json: files must be an object")
            else:
                for relative, record in lock_files.items():
                    if (
                        not isinstance(relative, str)
                        or Path(relative).is_absolute()
                        or ".." in Path(relative).parts
                    ):
                        errors.append(f".drydock-lock.json: unsafe path {relative!r}")
                        continue
                    if not isinstance(record, dict):
                        errors.append(f".drydock-lock.json: invalid record for {relative}")
                        continue
                    if record.get("ownership") not in VALID_OWNERSHIP:
                        errors.append(f".drydock-lock.json: invalid ownership for {relative}")
                    digest = record.get("sha256")
                    if not isinstance(digest, str) or not re.fullmatch(
                        r"[0-9a-f]{64}", digest
                    ):
                        errors.append(f".drydock-lock.json: invalid sha256 for {relative}")
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"Invalid .drydock-lock.json: {exc}")

    try:
        adapter_config = _adapter_config(root)
        if not isinstance(adapter_config, dict):
            errors.append("00-drydock/adapter.json: root must be an object")
            adapter_config = {}
        adapter_name = adapter_config.get("adapter")
        adapter_version = adapter_config.get("adapter_version")
        if not isinstance(adapter_name, str) or not adapter_name:
            errors.append("00-drydock/adapter.json: invalid or missing adapter")
        if not isinstance(adapter_version, str) or not adapter_version:
            errors.append("00-drydock/adapter.json: invalid or missing adapter_version")
        if manifest_data and adapter_name != manifest_data.get("adapter"):
            errors.append(".drydock.json: adapter does not match adapter.json")
        if manifest_data and adapter_version != manifest_data.get("adapter_version"):
            errors.append(".drydock.json: adapter_version does not match adapter.json")
        if lock and adapter_version != lock.get("adapter_version"):
            errors.append(".drydock-lock.json: adapter_version does not match adapter.json")
        connection_config = adapter_config.get("connections", {})
        if not isinstance(connection_config, dict):
            errors.append("00-drydock/adapter.json: connections must be an object")
            connection_config = {}
        configured_states = connection_config.get(
            "states", sorted(VALID_CONNECTION_STATES)
        )
        if (
            not isinstance(configured_states, list)
            or not configured_states
            or any(not isinstance(value, str) or not value for value in configured_states)
        ):
            errors.append(
                "00-drydock/adapter.json: connections.states must be a "
                "non-empty string list"
            )
            configured_states = sorted(VALID_CONNECTION_STATES)
        states = set(configured_states)
        vocabulary = connection_config.get("relationships", {})
        if (
            not isinstance(vocabulary, dict)
            or any(
                not isinstance(name, str)
                or not ID_PATTERN.fullmatch(name)
                or not isinstance(rule, dict)
                for name, rule in (
                    vocabulary.items() if isinstance(vocabulary, dict) else []
                )
            )
        ):
            errors.append(
                "00-drydock/adapter.json: connections.relationships must map "
                "kebab-case names to objects"
            )
            vocabulary = {}
        validation_rules = adapter_config.get("validation", {})
        if not isinstance(validation_rules, dict):
            errors.append("00-drydock/adapter.json: validation must be an object")
            validation_rules = {}
        field_values = validation_rules.get("field_values", {})
        if not isinstance(field_values, dict) or any(
            not isinstance(field, str)
            or not isinstance(values, list)
            or not values
            or any(not isinstance(value, str) for value in values)
            for field, values in (
                field_values.items() if isinstance(field_values, dict) else []
            )
        ):
            errors.append(
                "00-drydock/adapter.json: validation.field_values must map fields "
                "to non-empty string lists"
            )
            field_values = {}
        forbidden_combinations = validation_rules.get("forbidden_combinations", [])
        if not isinstance(forbidden_combinations, list) or any(
            not isinstance(combination, dict)
            or not combination
            or any(
                not isinstance(field, str) or not isinstance(value, str)
                for field, value in combination.items()
            )
            for combination in (
                forbidden_combinations
                if isinstance(forbidden_combinations, list)
                else []
            )
        ):
            errors.append(
                "00-drydock/adapter.json: validation.forbidden_combinations must "
                "be a list of field-value objects"
            )
            forbidden_combinations = []
        entity_types = adapter_config.get("entity_types", {})
        if not isinstance(entity_types, dict):
            errors.append("00-drydock/adapter.json: entity_types must be an object")
            entity_types = {}
        for entity_type, rule in entity_types.items():
            if not isinstance(rule, dict):
                errors.append(
                    f"00-drydock/adapter.json: entity type {entity_type} must be an object"
                )
                continue
            template = rule.get("template")
            destination = rule.get("destination")
            for label, declared_path in (
                ("template", template),
                ("destination", destination),
            ):
                if (
                    not isinstance(declared_path, str)
                    or not declared_path
                    or Path(declared_path).is_absolute()
                    or ".." in Path(declared_path).parts
                ):
                    errors.append(
                        f"00-drydock/adapter.json: {entity_type}.{label} is unsafe"
                    )
            if isinstance(destination, str) and "{id}" not in destination:
                errors.append(
                    f"00-drydock/adapter.json: {entity_type}.destination must contain {{id}}"
                )
            required_values = rule.get("required_values", {})
            if not isinstance(required_values, dict) or any(
                not isinstance(field, str) or not isinstance(value, str)
                for field, value in (
                    required_values.items() if isinstance(required_values, dict) else []
                )
            ):
                errors.append(
                    f"00-drydock/adapter.json: {entity_type}.required_values must "
                    "be a field-value object"
                )
            required_fields = rule.get("required_fields", [])
            if not isinstance(required_fields, list) or any(
                not isinstance(field, str) for field in required_fields
            ):
                errors.append(
                    f"00-drydock/adapter.json: {entity_type}.required_fields "
                    "must be a string list"
                )
            nonempty_fields = rule.get("nonempty_fields", [])
            if not isinstance(nonempty_fields, list) or any(
                not isinstance(field, str) for field in nonempty_fields
            ):
                errors.append(
                    f"00-drydock/adapter.json: {entity_type}.nonempty_fields "
                    "must be a string list"
                )
            forbidden_headings = rule.get("forbidden_headings", [])
            if not isinstance(forbidden_headings, list) or any(
                not isinstance(heading, str) for heading in forbidden_headings
            ):
                errors.append(
                    f"00-drydock/adapter.json: {entity_type}.forbidden_headings "
                    "must be a string list"
                )
        legacy_paths = adapter_config.get("legacy_paths", [])
        if not isinstance(legacy_paths, list) or any(
            not isinstance(rule, dict)
            or not isinstance(rule.get("path"), str)
            or not isinstance(rule.get("canonical"), str)
            or not rule.get("path")
            or not rule.get("canonical")
            for rule in (legacy_paths if isinstance(legacy_paths, list) else [])
        ):
            errors.append(
                "00-drydock/adapter.json: legacy_paths must declare path and canonical"
            )
            legacy_paths = []
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"Invalid 00-drydock/adapter.json: {exc}")
        entity_types = {}
        field_values = {}
        forbidden_combinations = []
        legacy_paths = []
        states = VALID_CONNECTION_STATES
        vocabulary = {}

    files = list(root.rglob("*.md"))
    for rule in legacy_paths:
        legacy_root = root / rule["path"]
        candidates = legacy_root.glob("*.md") if rule.get("direct_files_only") else legacy_root.rglob("*.md")
        for legacy_file in candidates:
            warnings.append(
                f"{legacy_file.relative_to(root)}: legacy adapter path; optional manual "
                f"move to {rule['canonical']} after reviewing links"
            )
    known = {path.stem.lower() for path in files} | {
        path.relative_to(root).with_suffix("").as_posix().lower() for path in files
    }
    known |= {
        entity_id.lower()
        for path in files
        if (entity_id := frontmatter(path.read_text(encoding="utf-8")).get("id"))
    }
    for path in files:
        text = path.read_text(encoding="utf-8")
        metadata = frontmatter(text)
        relative = path.relative_to(root)
        status = metadata.get("status")
        if status and status not in VALID_STATUSES:
            errors.append(f"{relative}: invalid status {status}")
        ownership = metadata.get("ownership")
        if ownership and ownership not in VALID_OWNERSHIP:
            errors.append(f"{relative}: invalid ownership {ownership}")
        for field, allowed_values in field_values.items():
            value = metadata.get(field)
            if value is not None and value not in allowed_values:
                errors.append(f"{relative}: invalid {field} {value}")
        for combination in forbidden_combinations:
            if all(metadata.get(field) == value for field, value in combination.items()):
                rendered = ", ".join(
                    f"{field}={value}" for field, value in combination.items()
                )
                errors.append(f"{relative}: forbidden field combination {rendered}")
        entity_type = metadata.get("type")
        entity_rule = entity_types.get(entity_type, {})
        if not isinstance(entity_rule, dict):
            entity_rule = {}
        if (
            entity_rule
            and metadata.get("ownership") == "campaign"
            and not relative.as_posix().startswith("templates/")
        ):
            for field in entity_rule.get("required_fields", []):
                if field not in metadata:
                    errors.append(f"{relative}: missing required field {field}")
            for field in entity_rule.get("nonempty_fields", []):
                if not metadata.get(field, "").strip():
                    errors.append(f"{relative}: field {field} must not be empty")
            for field, required_value in entity_rule.get("required_values", {}).items():
                if metadata.get(field) != required_value:
                    errors.append(
                        f"{relative}: {field} must be {required_value} for {entity_type}"
                    )
            headings = {heading.casefold() for heading in HEADING_PATTERN.findall(text)}
            for heading in entity_rule.get("forbidden_headings", []):
                if heading.casefold() in headings:
                    errors.append(f"{relative}: forbidden heading {heading}")
        entity_id = metadata.get("id")
        if entity_id:
            if entity_id in ids:
                errors.append(f"{relative}: duplicate ID {entity_id}")
            ids[entity_id] = relative
        for raw in re.findall(r"\[\[([^\]]+)\]\]", text):
            target = raw.split("|", 1)[0].split("#", 1)[0].strip().lower()
            if target and target != "target-id" and target not in known:
                warnings.append(f"{relative}: unresolved wikilink [[{raw}]]")
        if any(marker in text for marker in ("<<<<<<<", "=======", ">>>>>>>")):
            errors.append(f"{relative}: merge conflict marker")

    entities, connections, connection_errors = _graph(root)
    errors.extend(connection_errors)
    seen_edges: set[tuple[str, str, str, str]] = set()
    for connection in connections:
        target = entities.get(connection.target_id)
        source = entities.get(connection.source_id)
        location = f"{connection.path}:{connection.line}"
        if target is None:
            errors.append(f"{location}: connection target {connection.target_id} does not exist")
            continue
        if connection.state not in states:
            errors.append(f"{location}: invalid connection state {connection.state}")
        if connection.source_id == connection.target_id:
            warnings.append(f"{location}: self-connection")
        edge = (connection.source_id, connection.target_id, connection.relationship, connection.state)
        if edge in seen_edges:
            warnings.append(f"{location}: duplicate connection")
        seen_edges.add(edge)
        if isinstance(vocabulary, dict) and connection.relationship not in vocabulary:
            warnings.append(f"{location}: unknown relationship {connection.relationship}")
        if source and source.visibility == "players" and target.visibility == "warden":
            errors.append(f"{location}: player-visible connection exposes Warden-only target {target.entity_id}")
        if len(connection.context) > 200:
            warnings.append(f"{location}: connection context exceeds 200 characters")

    print(f"Checked {len(files)} Markdown files.")
    for warning in warnings:
        print("WARNING:", warning)
    for error in errors:
        print("ERROR:", error)
    if errors:
        return 1
    print(f"Validation passed with {len(warnings)} warning(s).")
    return 0


def create_entity(root: Path, kind: str, entity_id: str, name: str | None) -> Path:
    root = root.resolve()
    if not ID_PATTERN.fullmatch(entity_id):
        raise SystemExit("Entity ID must use lowercase letters, numbers, and hyphens")
    config = _adapter_config(root)
    rule = config.get("entity_types", {}).get(kind)
    if not isinstance(rule, dict):
        available = ", ".join(sorted(config.get("entity_types", {}))) or "none"
        raise SystemExit(f"Unknown entity type {kind!r}; available types: {available}")
    template = (root / rule["template"]).resolve()
    destination = (root / rule["destination"].format(id=entity_id)).resolve()
    if not template.is_relative_to(root) or not destination.is_relative_to(root):
        raise SystemExit("Adapter entity paths must remain inside the campaign")
    if destination.exists():
        raise SystemExit(f"Refusing to overwrite existing entity: {destination}")
    for existing in root.rglob("*.md"):
        metadata = frontmatter(existing.read_text(encoding="utf-8"))
        if metadata.get("id") == entity_id:
            raise SystemExit(
                f"Refusing duplicate entity ID {entity_id}: {existing.relative_to(root)}"
            )
    text = template.read_text(encoding="utf-8")
    text = re.sub(r"(?m)^id:\s*.*$", f"id: {entity_id}", text, count=1)
    text = re.sub(r"(?m)^ownership:\s*.*$", "ownership: campaign", text, count=1)
    if name is not None:
        if re.search(r"(?m)^name:", text):
            escaped_name = name.replace('"', '\\"')
            text = re.sub(
                r"(?m)^name:\s*.*$", f'name: "{escaped_name}"', text, count=1
            )
        text = re.sub(r"(?m)^# (Name|Adventure|Session)$", f"# {name}", text, count=1)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")
    print(f"Created {destination.relative_to(root)}")
    return destination


def _approved_session_logs(root: Path) -> list[Path]:
    logs = []
    for path in (root / "12-sessions" / "logs").glob("*.md"):
        metadata = frontmatter(path.read_text(encoding="utf-8"))
        if metadata.get("status") in APPROVED_SESSION_STATUSES:
            logs.append(path)
    return sorted(logs)


def _summary(entity: Entity) -> str:
    lines = [line.strip() for _, line in _section_lines(entity.text, "Summary") if line.strip() and not line.startswith("<!--")]
    return " ".join(lines) or "No summary recorded."


def build_indexes(root: Path) -> tuple[Path, Path]:
    root = root.resolve()
    entities, connections = _graph_or_exit(root)
    outgoing: dict[str, list[Connection]] = {key: [] for key in entities}
    incoming: dict[str, list[Connection]] = {key: [] for key in entities}
    for connection in connections:
        outgoing.setdefault(connection.source_id, []).append(connection)
        incoming.setdefault(connection.target_id, []).append(connection)
    entity_lines = ["# Entity Index", "", "> Generated by Warden Drydock. Do not edit manually.", ""]
    for entity_type in sorted({entity.entity_type for entity in entities.values()}):
        entity_lines += [f"## {entity_type.replace('-', ' ').title()}", ""]
        for entity in sorted((item for item in entities.values() if item.entity_type == entity_type), key=lambda item: item.entity_id):
            links = "; ".join(f"{edge.relationship} [[{edge.target_id}]]" for edge in sorted(outgoing.get(entity.entity_id, []), key=lambda edge: (edge.relationship, edge.target_id)) if edge.state not in {"former", "inactive"})
            suffix = f" Connections: {links}." if links else ""
            entity_lines.append(f"- [[{entity.entity_id}|{entity.name}]] — {entity.status}; {_summary(entity)}{suffix} Source: `{entity.path.as_posix()}`")
        entity_lines.append("")
    entity_path = root / "00-drydock" / "entity-index.md"
    entity_path.write_text("\n".join(entity_lines).rstrip() + "\n", encoding="utf-8")

    connection_lines = ["# Connection Index", "", "> Generated by Warden Drydock. Backlinks are derived; edit source records instead.", ""]
    for entity in sorted(entities.values(), key=lambda item: item.entity_id):
        connection_lines += [f"## [[{entity.entity_id}|{entity.name}]]", ""]
        for edge in sorted(outgoing.get(entity.entity_id, []), key=lambda item: (item.relationship, item.target_id)):
            connection_lines.append(f"- outgoing `{edge.relationship}` → [[{edge.target_id}]] (`{edge.state}`) — {edge.context}")
        for edge in sorted(incoming.get(entity.entity_id, []), key=lambda item: (item.relationship, item.source_id)):
            connection_lines.append(f"- backlink `{edge.relationship}` ← [[{edge.source_id}]] (`{edge.state}`) — {edge.context}")
        if not outgoing.get(entity.entity_id) and not incoming.get(entity.entity_id):
            connection_lines.append("- No explicit connections.")
        connection_lines.append("")
    connection_path = root / "00-drydock" / "connection-index.md"
    connection_path.write_text("\n".join(connection_lines).rstrip() + "\n", encoding="utf-8")
    print(f"Wrote {entity_path.relative_to(root)}")
    print(f"Wrote {connection_path.relative_to(root)}")
    return entity_path, connection_path


def related_entities(root: Path, focus: str, depth: int = 1) -> list[Entity]:
    if depth < 0:
        raise SystemExit("Depth must be zero or greater")
    entities, connections = _graph_or_exit(root)
    if focus not in entities:
        raise SystemExit(f"Unknown entity ID {focus!r}")
    selected = {focus}
    frontier = {focus}
    for _ in range(max(0, depth)):
        neighbors = {edge.target_id for edge in connections if edge.source_id in frontier}
        neighbors |= {edge.source_id for edge in connections if edge.target_id in frontier}
        neighbors &= entities.keys()
        frontier = neighbors - selected
        selected |= frontier
    ordered = [focus] + sorted(selected - {focus})
    return [entities[key] for key in ordered]


def build_context(root: Path, *, focus: str | None = None, depth: int = 1,
                  max_records: int = 20) -> Path:
    if depth < 0:
        raise SystemExit("Depth must be zero or greater")
    if max_records <= 0:
        raise SystemExit("Maximum records must be greater than zero")
    root = root.resolve()
    output = root / "00-drydock" / "ai-context.md"
    sources = [
        root / "00-drydock" / "current-state.md",
        root / "00-drydock" / "canon-policy.md",
        root / "00-drydock" / "system-principles.md",
    ]
    parts = [
        "# AI Context",
        "",
        "> Generated by Warden Drydock. Do not edit manually.",
        "",
    ]
    for path in sources:
        if path.exists():
            parts += [
                f"## {path.stem.replace('-', ' ').title()}",
                "",
                body(path.read_text(encoding="utf-8")),
                "",
            ]
    logs = _approved_session_logs(root)
    parts += [
        "## Latest Approved Session",
        "",
        body(logs[-1].read_text(encoding="utf-8"))
        if logs
        else "No approved session log recorded.",
        "",
    ]
    entities = related_entities(root, focus, depth) if focus else []
    included = entities[:max_records]
    if included:
        parts += ["## Focused Records", ""]
        for entity in included:
            parts += [f"### {entity.name} (`{entity.entity_id}`)", "", body(entity.text), ""]
        omitted = len(entities) - len(included)
        parts += ["## Retrieval Report", "", f"Included {len(included)} record(s) for `{focus}` at depth {depth}."]
        if omitted:
            parts.append(f"Omitted {omitted} record(s) because of `--max-records {max_records}`; use `related {focus}` to inspect them.")
        parts.append("")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(parts).rstrip() + "\n", encoding="utf-8")
    print(f"Wrote {output.relative_to(root)}")
    return output


def print_entities(root: Path, query: str | None = None) -> None:
    entities = _entities(root.resolve())
    matches = entities.values()
    if query:
        needle = query.casefold()
        matches = [entity for entity in matches if needle in entity.entity_id.casefold() or needle in entity.name.casefold() or needle in _summary(entity).casefold()]
    for entity in sorted(matches, key=lambda item: item.entity_id):
        print(f"{entity.entity_id}\t{entity.entity_type}\t{entity.name}\t{entity.path.as_posix()}")


def print_related(root: Path, focus: str, depth: int) -> None:
    for entity in related_entities(root.resolve(), focus, depth):
        print(f"{entity.entity_id}\t{entity.entity_type}\t{entity.name}\t{entity.path.as_posix()}")


def print_entity(root: Path, entity_id: str) -> None:
    entities = _entities(root.resolve())
    if entity_id not in entities:
        raise SystemExit(f"Unknown entity ID {entity_id!r}")
    print(entities[entity_id].text, end="")


def print_backlinks(root: Path, focus: str) -> None:
    entities, edges = _graph_or_exit(root.resolve())
    if focus not in entities:
        raise SystemExit(f"Unknown entity ID {focus!r}")
    for edge in edges:
        if edge.target_id == focus:
            print(f"{edge.source_id}\t{edge.relationship}\t{edge.state}\t{edge.context}")


def print_history(root: Path, focus: str) -> None:
    entities, connections = _graph_or_exit(root.resolve())
    if focus not in entities:
        raise SystemExit(f"Unknown entity ID {focus!r}")
    historical = {"session", "debrief", "faction-turn", "consequence"}
    for edge in connections:
        source = entities.get(edge.source_id)
        if edge.target_id == focus and source and source.entity_type in historical:
            print(f"{source.entity_id}\t{edge.relationship}\t{edge.context}\t{source.path.as_posix()}")


def audit_connections(root: Path) -> int:
    count = 0
    for entity in _entities(root.resolve()).values():
        metadata = frontmatter(entity.text)
        for field, relationship in (("factions", "affiliated-with"), ("locations", "located-at"), ("characters", "features")):
            raw = metadata.get(field)
            if raw and raw != "[]":
                print(f"PROPOSED {entity.path.as_posix()}: {entity.entity_id} --{relationship}--> {raw}; evidence: legacy frontmatter field {field}; review required")
                count += 1
    print(f"Found {count} explicit legacy connection proposal(s); no campaign files changed.")
    return 0


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def main(argv: list[str] | None = None, *, root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description="Warden Drydock campaign maintenance")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate")
    context = subparsers.add_parser("context")
    context.add_argument("--focus")
    context.add_argument("--depth", type=_nonnegative_int, default=1)
    context.add_argument("--max-records", type=_positive_int, default=20)
    subparsers.add_parser("index", help="Rebuild generated entity and connection indexes")
    find = subparsers.add_parser("find", help="Find entities by ID, name, or summary")
    find.add_argument("query")
    show = subparsers.add_parser("show", help="Print one entity record")
    show.add_argument("entity_id")
    related = subparsers.add_parser("related", help="List a connected entity neighborhood")
    related.add_argument("entity_id")
    related.add_argument("--depth", type=_nonnegative_int, default=1)
    backlinks = subparsers.add_parser("backlinks", help="List records that connect to an entity")
    backlinks.add_argument("entity_id")
    history = subparsers.add_parser("history", help="List historical records connected to an entity")
    history.add_argument("entity_id")
    connections = subparsers.add_parser("connections", help="Audit legacy relationship fields")
    connections.add_argument("action", choices=["audit"])
    new = subparsers.add_parser("new", help="Create an entity from an adapter template")
    new.add_argument("kind")
    new.add_argument("entity_id")
    new.add_argument("--name")
    args = parser.parse_args(argv)
    campaign_root = root or Path(__file__).resolve().parents[1]
    if args.command == "validate":
        return validate_campaign(campaign_root)
    if args.command == "context":
        build_indexes(campaign_root)
        build_context(campaign_root, focus=args.focus, depth=args.depth,
                      max_records=args.max_records)
        return 0
    if args.command == "index":
        build_indexes(campaign_root)
        return 0
    if args.command == "find":
        print_entities(campaign_root, args.query)
        return 0
    if args.command == "show":
        print_entity(campaign_root, args.entity_id)
        return 0
    if args.command == "related":
        print_related(campaign_root, args.entity_id, args.depth)
        return 0
    if args.command == "backlinks":
        print_backlinks(campaign_root, args.entity_id)
        return 0
    if args.command == "history":
        print_history(campaign_root, args.entity_id)
        return 0
    if args.command == "connections":
        return audit_connections(campaign_root)
    create_entity(campaign_root, args.kind, args.entity_id, args.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
