"""The object-store interface: the Parquet history, the backups and the dashboard's snapshot."""

from __future__ import annotations

from typing import Protocol

_FORBIDDEN_PARTS = {"", ".", ".."}


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> None: ...

    def get(self, key: str) -> bytes:
        """Raises `KeyError` when the key doesn't exist."""
        ...

    def exists(self, key: str) -> bool: ...

    def list(self, prefix: str = "") -> list[str]:
        """Keys under `prefix`, sorted."""
        ...


def validate_key(key: str) -> str:
    """Keys are relative, `/`-separated paths with no empty, `.` or `..` parts."""
    if key.startswith("/") or "\\" in key:
        raise ValueError(f"invalid storage key: {key!r}")
    if any(part in _FORBIDDEN_PARTS for part in key.split("/")):
        raise ValueError(f"invalid storage key: {key!r}")
    return key


def validate_prefix(prefix: str) -> str:
    if prefix:
        validate_key(prefix.removesuffix("/"))
    return prefix
