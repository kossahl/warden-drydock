from __future__ import annotations

from pathlib import Path
import shutil
import threading

from .models import WorkspaceHandle


class UnknownWorkspaceError(LookupError):
    pass


class UnsafeWorkspaceError(RuntimeError):
    pass


class WorkspaceRegistry:
    """Server-owned in-memory handle registry rooted in private scratch storage."""

    def __init__(self, storage_root: Path) -> None:
        self._root = storage_root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._paths: dict[WorkspaceHandle, Path] = {}
        self._counter = 0
        self._lock = threading.Lock()

    def allocate(self) -> WorkspaceHandle:
        with self._lock:
            self._counter += 1
            handle = WorkspaceHandle(f"workspace_{self._counter:08d}")
            path = self._root / handle.value
            path.mkdir()
            self._paths[handle] = path
            return handle

    def clone(self, source: WorkspaceHandle) -> WorkspaceHandle:
        source_path = self._resolve(source)
        target = self.allocate()
        target_path = self._paths[target]
        try:
            shutil.copytree(source_path, target_path, dirs_exist_ok=True)
            self._assert_safe_tree(target_path)
        except Exception:
            self.discard(target)
            raise
        return target

    def discard(self, handle: WorkspaceHandle) -> None:
        path = self._paths.get(handle)
        if path is not None:
            self._assert_safe_tree(path)
            self._paths.pop(handle)
            shutil.rmtree(path)

    def _resolve(self, handle: WorkspaceHandle) -> Path:
        try:
            path = self._paths[handle]
        except KeyError as exc:
            raise UnknownWorkspaceError(handle.value) from exc
        self._assert_safe_tree(path)
        return path

    def _assert_safe_tree(self, root: Path) -> None:
        resolved = root.resolve()
        if not resolved.is_relative_to(self._root) or resolved == self._root:
            raise UnsafeWorkspaceError("workspace escaped server storage")
        for candidate in root.rglob("*"):
            if candidate.is_symlink():
                raise UnsafeWorkspaceError("workspace contains a symbolic link")
