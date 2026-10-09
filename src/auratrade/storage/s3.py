from __future__ import annotations

from typing import TYPE_CHECKING

from botocore.exceptions import ClientError

from auratrade.storage.base import validate_key, validate_prefix

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

_NOT_FOUND = {"404", "NoSuchKey", "NotFound"}


class S3Store:
    def __init__(self, bucket: str, client: S3Client, *, prefix: str = "") -> None:
        self.bucket = bucket
        self._client = client
        self._prefix = validate_prefix(prefix)

    def _key(self, key: str) -> str:
        return self._prefix + validate_key(key)

    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        if content_type is None:
            self._client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data)
        else:
            self._client.put_object(
                Bucket=self.bucket, Key=self._key(key), Body=data, ContentType=content_type
            )

    def get(self, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=self._key(key))
        except ClientError as exc:
            if _is_not_found(exc):
                raise KeyError(key) from None
            raise
        return response["Body"].read()

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=self._key(key))
        except ClientError as exc:
            if _is_not_found(exc):
                return False
            raise
        return True

    def list(self, prefix: str = "") -> list[str]:
        full = self._prefix + validate_prefix(prefix)
        keys: list[str] = []
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=full):
            keys.extend(obj["Key"].removeprefix(self._prefix) for obj in page.get("Contents", []))
        return sorted(keys)


def _is_not_found(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") in _NOT_FOUND
