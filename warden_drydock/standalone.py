"""Portable campaign maintenance commands.

This module intentionally uses only the Python standard library. The generator
copies it verbatim into campaign repositories as ``scripts/drydock.py`` while
the framework imports the same implementation.
"""

from __future__ import annotations

import argparse
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
        if entity_rule and not relative.as_posix().startswith("templates/"):
            for field in entity_rule.get("required_fields", []):
                if field not in metadata:
                    errors.append(f"{relative}: missing required field {field}")
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
            if target and target not in known:
                warnings.append(f"{relative}: unresolved wikilink [[{raw}]]")
        if any(marker in text for marker in ("<<<<<<<", "=======", ">>>>>>>")):
            errors.append(f"{relative}: merge conflict marker")

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


def build_context(root: Path) -> Path:
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
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(parts).rstrip() + "\n", encoding="utf-8")
    print(f"Wrote {output.relative_to(root)}")
    return output


def main(argv: list[str] | None = None, *, root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description="Warden Drydock campaign maintenance")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate")
    subparsers.add_parser("context")
    new = subparsers.add_parser("new", help="Create an entity from an adapter template")
    new.add_argument("kind")
    new.add_argument("entity_id")
    new.add_argument("--name")
    args = parser.parse_args(argv)
    campaign_root = root or Path(__file__).resolve().parents[1]
    if args.command == "validate":
        return validate_campaign(campaign_root)
    if args.command == "context":
        build_context(campaign_root)
        return 0
    create_entity(campaign_root, args.kind, args.entity_id, args.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
