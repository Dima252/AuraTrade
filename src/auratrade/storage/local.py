from __future__ import annotations

from pathlib import Path

from auratrade.storage.base import validate_key, validate_prefix


class LocalStore:
    """A folder on disk with the same semantics as the S3 store (local runs and tests)."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _path(self, key: str) -> Path:
        return self.root.joinpath(*validate_key(key).split("/"))

    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write then rename, so a reader never sees a half-written object.
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise KeyError(key)
        return path.read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def list(self, prefix: str = "") -> list[str]:
        validate_prefix(prefix)
        if not self.root.is_dir():
            return []
        keys = (
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*")
            if path.is_file() and not path.name.startswith(".")
        )
        return sorted(key for key in keys if key.startswith(prefix))
