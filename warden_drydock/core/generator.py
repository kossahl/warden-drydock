from __future__ import annotations
from pathlib import Path
import json
import shutil

PACKAGE = Path(__file__).resolve().parents[1]
DATA = PACKAGE / "data"


def _copy_tree(source: Path, destination: Path) -> None:
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def _render(root: Path, values: dict[str, str]) -> None:
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in {".md", ".yaml", ".yml", ".json"}:
            continue
        text = path.read_text(encoding="utf-8")
        for key, value in values.items():
            text = text.replace("{{" + key + "}}", value)
        path.write_text(text, encoding="utf-8")


def init_campaign(path: Path, *, name: str, adapter: str, force: bool = False) -> None:
    path = path.resolve()
    if path.exists() and any(path.iterdir()) and not force:
        raise SystemExit(f"Refusing to initialize non-empty directory: {path}")
    path.mkdir(parents=True, exist_ok=True)
    _copy_tree(DATA / "project_template", path)
    _copy_tree(DATA / "adapters" / adapter, path)
    _render(path, {"campaign_name": name, "adapter": adapter})
    manifest = {
        "framework": "warden-drydock",
        "framework_version": "0.1.0",
        "adapter": adapter,
        "adapter_version": "0.1.0",
        "ownership_model": 1,
    }
    (path / ".drydock.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
