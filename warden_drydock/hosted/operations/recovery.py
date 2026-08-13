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
    names: set[str] = set()
    for member in members:
        path = pathlib.PurePosixPath(member.name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or not path.parts
            or path.parts[0] != "snapshots"
            or member.issym()
            or member.islnk()
            or not (member.isfile() or member.isdir())
            or member.name in names
        ):
            raise ValueError("unsafe_backup_member")
        names.add(member.name)
        accepted.append(member)
    return accepted


def create_snapshot_archive(source: pathlib.Path, destination: pathlib.Path) -> str:
    with tarfile.open(destination, "w") as archive:
        archive.add(source, arcname="snapshots", recursive=True)
    with tarfile.open(destination, "r") as archive:
        members = safe_members(archive.getmembers())
        identity = "".join(
            f"{member.name}:{hashlib.sha256(archive.extractfile(member).read()).hexdigest()}\n"
            for member in members if member.isfile()
        ).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()


def extract_snapshot_archive(archive_path: pathlib.Path, destination: pathlib.Path) -> pathlib.Path:
    staging = destination / "snapshot-restore-staging"
    staging.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive_path, "r") as archive:
        archive.extractall(staging, members=safe_members(archive.getmembers()), filter="data")
    return staging / "snapshots"


def snapshot_archive_inventory(archive_path: pathlib.Path) -> str:
    with tarfile.open(archive_path, "r") as archive:
        members = safe_members(archive.getmembers())
        identity = "".join(
            f"{member.name}:{hashlib.sha256(archive.extractfile(member).read()).hexdigest()}\n"
            for member in members if member.isfile()
        ).encode("utf-8")
    return hashlib.sha256(identity).hexdigest()


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
    inventory = manifest.get("snapshot_inventory_digest")
    if not isinstance(inventory, str) or len(inventory) != 64:
        raise ValueError("invalid_snapshot_inventory_digest")
    for name, expected in manifest.get("files", {}).items():
        path = root / name
        if not path.is_file() or sha256_file(path) != expected:
            raise ValueError("backup_digest_mismatch")
    if snapshot_archive_inventory(root / "snapshots.tar") != inventory:
        raise ValueError("snapshot_inventory_digest_mismatch")
    return manifest
