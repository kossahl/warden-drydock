from __future__ import annotations

import hashlib
import json
import pathlib
import tarfile
from collections.abc import Iterable


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_members(members: Iterable[tarfile.TarInfo]) -> list[tarfile.TarInfo]:
    accepted = []
    for member in members:
        path = pathlib.PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk():
            raise ValueError("unsafe_backup_member")
        accepted.append(member)
    return accepted


def build_manifest(
    postgres_dump: pathlib.Path,
    snapshot_archive: pathlib.Path,
    snapshot_inventory_digest: str,
) -> dict[str, object]:
    return {
        "format_version": 1,
        "schema_compatibility": 1,
        "snapshot_inventory_digest": snapshot_inventory_digest,
        "files": {
            postgres_dump.name: sha256_file(postgres_dump),
            snapshot_archive.name: sha256_file(snapshot_archive),
        },
        "secrets_included": False,
        "unsynchronized_browser_data_included": False,
    }


def verify_manifest(root: pathlib.Path) -> dict[str, object]:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format_version") != 1 or manifest.get("schema_compatibility") != 1:
        raise ValueError("unsupported_backup_format")
    for name, expected in manifest.get("files", {}).items():
        path = root / name
        if not path.is_file() or sha256_file(path) != expected:
            raise ValueError("backup_digest_mismatch")
    return manifest
