from __future__ import annotations
from pathlib import Path
import json
import re

VALID = {"idea", "draft", "review", "canon", "revealed", "archived", "accepted"}


def _frontmatter(text: str) -> dict[str, str]:
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


def validate_campaign(root: Path) -> int:
    root = root.resolve()
    errors: list[str] = []
    warnings: list[str] = []
    ids: dict[str, Path] = {}
    manifest = root / ".drydock.json"
    if not manifest.exists():
        errors.append("Missing .drydock.json")
    else:
        try:
            json.loads(manifest.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"Invalid .drydock.json: {exc}")

    files = list(root.rglob("*.md"))
    known = {path.stem.lower() for path in files} | {
        path.relative_to(root).with_suffix("").as_posix().lower() for path in files
    }
    for path in files:
        text = path.read_text(encoding="utf-8")
        frontmatter = _frontmatter(text)
        relative = path.relative_to(root)
        status = frontmatter.get("status")
        if status and status not in VALID:
            errors.append(f"{relative}: invalid status {status}")
        entity_id = frontmatter.get("id")
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
