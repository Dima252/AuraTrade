"""Object storage: local folder and S3."""

from __future__ import annotations

from typing import TYPE_CHECKING

from auratrade.storage.base import ObjectStore
from auratrade.storage.local import LocalStore
from auratrade.storage.s3 import S3Store

if TYPE_CHECKING:
    from auratrade.config import Settings

__all__ = ["LocalStore", "ObjectStore", "S3Store", "make_store"]


def make_store(settings: Settings) -> ObjectStore:
    if settings.storage_backend == "s3":
        import boto3

        if settings.storage_s3_bucket is None:  # Settings already rejects this
            raise ValueError("STORAGE_S3_BUCKET is not set")
        return S3Store(settings.storage_s3_bucket, boto3.client("s3"))
    return LocalStore(settings.storage_local_dir)
