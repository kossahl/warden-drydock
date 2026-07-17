from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from .generator import init_campaign


MANAGED_OWNERSHIP = {"framework", "adapter", "shared"}


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Cannot read {path.name}: {exc}") from exc


def _hash(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def upgrade_campaign(root: Path, *, apply: bool = False) -> int:
    root = root.resolve()
    manifest = _read_json(root / ".drydock.json")
    current_lock = _read_json(root / ".drydock-lock.json")
    if current_lock.get("schema_version") != 1:
        raise SystemExit("Unsupported or missing Drydock lock schema")

    name = manifest.get("campaign_name")
    adapter = manifest.get("adapter")
    if not isinstance(name, str) or not name.strip():
        raise SystemExit("Manifest is missing campaign_name")
    if not isinstance(adapter, str) or not adapter:
        raise SystemExit("Manifest is missing adapter")

    with TemporaryDirectory() as temporary:
        desired_root = Path(temporary) / "campaign"
        init_campaign(desired_root, name=name, adapter=adapter)
        desired_lock = _read_json(desired_root / ".drydock-lock.json")
        old_files = current_lock.get("files", {})
        desired_files = desired_lock.get("files", {})
        changes: list[str] = []
        conflicts: list[str] = []

        for relative, desired in desired_files.items():
            if desired.get("ownership") not in MANAGED_OWNERSHIP:
                continue
            old = old_files.get(relative)
            current_hash = _hash(root / relative)
            desired_hash = desired.get("sha256")
            old_hash = old.get("sha256") if isinstance(old, dict) else None
            if current_hash == desired_hash:
                continue
            if old_hash == desired_hash and current_hash is not None:
                continue
            if old_hash is None:
                if current_hash is None:
                    changes.append(relative)
                else:
                    conflicts.append(relative)
            elif current_hash is None or current_hash == old_hash:
                changes.append(relative)
            else:
                conflicts.append(relative)

        metadata_changed = (
            current_lock.get("framework_version") != desired_lock.get("framework_version")
            or current_lock.get("adapter_version") != desired_lock.get("adapter_version")
        )

        if conflicts:
            print("Upgrade blocked by locally modified managed files:")
            for relative in conflicts:
                print(f"CONFLICT: {relative}")
            print("No files were changed.")
            return 1

        action = "Would update" if not apply else "Updated"
        for relative in changes:
            print(f"{action}: {relative}")
        if metadata_changed:
            print(f"{action}: Drydock version metadata")
        if not changes and not metadata_changed:
            print("Campaign framework files are already current.")
            return 0
        if not apply:
            print("Preview only. Re-run with --apply to write these changes.")
            return 0

        for relative in changes:
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(desired_root / relative, destination)
        shutil.copy2(desired_root / ".drydock.json", root / ".drydock.json")
        shutil.copy2(desired_root / ".drydock-lock.json", root / ".drydock-lock.json")
        print(f"Applied {len(changes)} managed file update(s).")
        return 0
