from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath

from .models import FileHash, SnapshotIntegrityError, SnapshotManifest


MANIFEST_NAME = "snapshot-manifest-v1.json"


def _safe_relative_path(root: Path, candidate: Path) -> str:
    if candidate.is_symlink():
        raise SnapshotIntegrityError("snapshot tree contains a symbolic link")
    relative = candidate.relative_to(root).as_posix()
    parsed = PurePosixPath(relative)
    if (
        not relative
        or parsed.is_absolute()
        or ".." in parsed.parts
        or "\\" in relative
        or ":" in parsed.parts[0]
    ):
        raise SnapshotIntegrityError("snapshot tree contains an unsafe path")
    return relative


def canonicalize_tree(root: Path) -> tuple[tuple[FileHash, ...], str]:
    if root.is_symlink():
        raise SnapshotIntegrityError("snapshot root is a symbolic link")
    root = root.resolve(strict=True)
    files: list[FileHash] = []
    for candidate in root.rglob("*"):
        relative = _safe_relative_path(root, candidate)
        if candidate.is_dir():
            continue
        if not candidate.is_file():
            raise SnapshotIntegrityError("snapshot tree contains a non-regular file")
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        files.append(FileHash(relative, digest))
    files.sort(key=lambda item: item.relative_path)
    if not files:
        raise SnapshotIntegrityError("snapshot tree is empty")
    canonical = json.dumps(
        [{"relative_path": item.relative_path, "sha256": item.sha256} for item in files],
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return tuple(files), hashlib.sha256(canonical).hexdigest()


def manifest_payload(manifest: SnapshotManifest) -> dict[str, object]:
    return {
        "adapter_version": manifest.adapter_version,
        "campaign_id": manifest.campaign_id,
        "change_digest": manifest.change_digest,
        "files": [
            {"relative_path": item.relative_path, "sha256": item.sha256}
            for item in manifest.files
        ],
        "framework_version": manifest.framework_version,
        "manifest_version": manifest.manifest_version,
        "ordinal": manifest.ordinal,
        "parent_revision": manifest.parent_revision,
        "publication_intent_token": manifest.publication_intent_token,
        "revision_id": manifest.revision_id,
        "tree_digest": manifest.tree_digest,
        "validation_contract_digest": manifest.validation_contract_digest,
    }


def encode_manifest(manifest: SnapshotManifest) -> bytes:
    return (json.dumps(manifest_payload(manifest), sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def decode_manifest(data: bytes) -> SnapshotManifest:
    try:
        value = json.loads(data)
        required = {
            "adapter_version", "campaign_id", "change_digest", "files",
            "framework_version", "manifest_version", "ordinal", "parent_revision",
            "publication_intent_token", "revision_id", "tree_digest",
            "validation_contract_digest",
        }
        if set(value) != required or value["manifest_version"] != 1:
            raise ValueError
        files = tuple(FileHash(item["relative_path"], item["sha256"]) for item in value["files"])
        return SnapshotManifest(
            campaign_id=value["campaign_id"], revision_id=value["revision_id"],
            parent_revision=value["parent_revision"], ordinal=value["ordinal"],
            tree_digest=value["tree_digest"], files=files,
            framework_version=value["framework_version"], adapter_version=value["adapter_version"],
            validation_contract_digest=value["validation_contract_digest"],
            change_digest=value["change_digest"],
            publication_intent_token=value["publication_intent_token"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SnapshotIntegrityError("snapshot manifest is invalid") from exc
