from __future__ import annotations
from pathlib import Path
import hashlib
import json
import shutil

from warden_drydock import __version__

PACKAGE = Path(__file__).resolve().parents[1]
DATA = PACKAGE / "data"


def _copy_tree(
    source: Path,
    destination: Path,
    ownership: dict[str, str],
    *,
    default_ownership: str,
) -> None:
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            ownership[target.relative_to(destination).as_posix()] = default_ownership


def _render(root: Path, values: dict[str, str]) -> None:
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in {".md", ".yaml", ".yml", ".json"}:
            continue
        text = path.read_text(encoding="utf-8")
        for key, value in values.items():
            text = text.replace("{{" + key + "}}", value)
        path.write_text(text, encoding="utf-8")


def _declared_ownership(path: Path) -> str | None:
    if path.suffix != ".md":
        return None
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end < 0:
        return None
    for line in text[4:end].splitlines():
        if line.startswith("ownership:"):
            return line.split(":", 1)[1].strip().strip('"')
    return None


def _write_lock(path: Path, ownership: dict[str, str]) -> None:
    files = {}
    for relative, default in sorted(ownership.items()):
        target = path / relative
        declared = _declared_ownership(target)
        files[relative] = {
            "ownership": declared or default,
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        }
    lock = {
        "schema_version": 1,
        "framework_version": __version__,
        "adapter_version": "0.1.0",
        "files": files,
    }
    (path / ".drydock-lock.json").write_text(
        json.dumps(lock, indent=2) + "\n", encoding="utf-8"
    )


def init_campaign(path: Path, *, name: str, adapter: str, force: bool = False) -> None:
    path = path.resolve()
    if path.exists() and any(path.iterdir()) and not force:
        raise SystemExit(f"Refusing to initialize non-empty directory: {path}")
    path.mkdir(parents=True, exist_ok=True)
    ownership: dict[str, str] = {}
    _copy_tree(
        DATA / "project_template", path, ownership, default_ownership="framework"
    )
    ownership["00-drydock/ai-context.md"] = "generated"
    _copy_tree(DATA / "adapters" / adapter, path, ownership, default_ownership="adapter")
    (path / "scripts").mkdir(parents=True, exist_ok=True)
    shutil.copy2(PACKAGE / "standalone.py", path / "scripts" / "drydock.py")
    ownership["scripts/drydock.py"] = "framework"
    _render(path, {"campaign_name": name, "adapter": adapter})
    manifest = {
        "framework": "warden-drydock",
        "framework_version": __version__,
        "adapter": adapter,
        "adapter_version": "0.1.0",
        "ownership_model": 1,
        "campaign_name": name,
    }
    (path / ".drydock.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    _write_lock(path, ownership)
