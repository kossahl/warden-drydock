from __future__ import annotations

import hashlib
import os
import pathlib
import tempfile


class SecretStore:
    def __init__(self, root: pathlib.Path):
        self.root = root
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _path(self, name: str) -> pathlib.Path:
        if not name.isascii() or not name.replace("-", "").replace("_", "").isalnum():
            raise ValueError("unsafe_secret_name")
        return self.root / name

    def replace(self, name: str, value: bytes) -> str:
        if not value:
            raise ValueError("empty_secret")
        target = self._path(name)
        descriptor, temporary = tempfile.mkstemp(prefix=".replace-", dir=self.root)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return hashlib.sha256(value).hexdigest()

    def metadata(self, name: str) -> dict[str, object]:
        path = self._path(name)
        if not path.exists():
            return {"present": False, "credential_revision": None}
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return {"present": True, "credential_revision": digest}

    def remove(self, name: str) -> None:
        self._path(name).unlink(missing_ok=True)
