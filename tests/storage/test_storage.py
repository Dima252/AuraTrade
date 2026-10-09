import io
from pathlib import Path

import boto3
import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber

from auratrade.config import Settings
from auratrade.storage import LocalStore, S3Store, make_store
from auratrade.storage.base import validate_key


@pytest.mark.parametrize("key", ["", "/abs", "a//b", "a/../b", "./a", "a/", "..", "a\\b", "a/./b"])
def test_invalid_keys(key: str) -> None:
    with pytest.raises(ValueError, match="invalid storage key"):
        validate_key(key)


class TestLocalStore:
    def test_round_trip_and_listing(self, tmp_path: Path) -> None:
        store = LocalStore(tmp_path / "root")
        assert store.list() == []
        store.put("charts/2026-10-07/ANET.png", b"png")
        store.put("backup/2026-10-07/trade_journal.parquet", b"pq")
        assert store.get("charts/2026-10-07/ANET.png") == b"png"
        assert store.exists("charts/2026-10-07/ANET.png")
        assert not store.exists("charts/2026-10-07/MSFT.png")
        assert store.list() == [
            "backup/2026-10-07/trade_journal.parquet",
            "charts/2026-10-07/ANET.png",
        ]
        assert store.list("charts/") == ["charts/2026-10-07/ANET.png"]

    def test_overwrite_leaves_no_temp_file(self, tmp_path: Path) -> None:
        store = LocalStore(tmp_path)
        store.put("a/b.txt", b"1")
        store.put("a/b.txt", b"2")
        assert store.get("a/b.txt") == b"2"
        assert sorted(p.name for p in (tmp_path / "a").iterdir()) == ["b.txt"]

    def test_missing_key(self, tmp_path: Path) -> None:
        with pytest.raises(KeyError):
            LocalStore(tmp_path).get("nope")

    def test_rejects_traversal(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="invalid storage key"):
            LocalStore(tmp_path).put("../escape", b"x")
        with pytest.raises(ValueError, match="invalid storage key"):
            LocalStore(tmp_path).list("../")


@pytest.fixture
def s3() -> tuple[S3Store, Stubber]:
    client = boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    stubber = Stubber(client)
    stubber.activate()
    return S3Store("bucket", client, prefix="paper/"), stubber


class TestS3Store:
    def test_put_with_and_without_content_type(self, s3: tuple[S3Store, Stubber]) -> None:
        store, stub = s3
        stub.add_response(
            "put_object", {}, {"Bucket": "bucket", "Key": "paper/a.png", "Body": b"x"}
        )
        stub.add_response(
            "put_object",
            {},
            {"Bucket": "bucket", "Key": "paper/b.png", "Body": b"y", "ContentType": "image/png"},
        )
        store.put("a.png", b"x")
        store.put("b.png", b"y", content_type="image/png")
        stub.assert_no_pending_responses()

    def test_get(self, s3: tuple[S3Store, Stubber]) -> None:
        store, stub = s3
        body = StreamingBody(io.BytesIO(b"data"), 4)
        stub.add_response("get_object", {"Body": body}, {"Bucket": "bucket", "Key": "paper/k"})
        assert store.get("k") == b"data"

    def test_get_missing_is_key_error(self, s3: tuple[S3Store, Stubber]) -> None:
        store, stub = s3
        stub.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)
        with pytest.raises(KeyError):
            store.get("k")

    def test_get_other_error_propagates(self, s3: tuple[S3Store, Stubber]) -> None:
        store, stub = s3
        stub.add_client_error("get_object", service_error_code="AccessDenied", http_status_code=403)
        with pytest.raises(Exception, match="AccessDenied"):
            store.get("k")

    def test_exists(self, s3: tuple[S3Store, Stubber]) -> None:
        store, stub = s3
        stub.add_response("head_object", {}, {"Bucket": "bucket", "Key": "paper/k"})
        stub.add_client_error("head_object", service_error_code="404", http_status_code=404)
        stub.add_client_error("head_object", service_error_code="403", http_status_code=403)
        assert store.exists("k")
        assert not store.exists("k")
        with pytest.raises(Exception, match="403"):
            store.exists("k")

    def test_list_strips_prefix_across_pages(self, s3: tuple[S3Store, Stubber]) -> None:
        store, stub = s3
        stub.add_response(
            "list_objects_v2",
            {
                "Contents": [{"Key": "paper/c/2.png"}],
                "IsTruncated": True,
                "NextContinuationToken": "t",
            },
            {"Bucket": "bucket", "Prefix": "paper/c/"},
        )
        stub.add_response(
            "list_objects_v2",
            {"Contents": [{"Key": "paper/c/1.png"}], "IsTruncated": False},
            {"Bucket": "bucket", "Prefix": "paper/c/", "ContinuationToken": "t"},
        )
        stub.add_response("list_objects_v2", {"IsTruncated": False})
        assert store.list("c/") == ["c/1.png", "c/2.png"]
        assert store.list("x/") == []


def test_make_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    local = make_store(Settings(_env_file=None, storage_local_dir=tmp_path))  # type: ignore[call-arg]
    assert isinstance(local, LocalStore)
    s3 = make_store(
        Settings(_env_file=None, storage_backend="s3", storage_s3_bucket="b")  # type: ignore[call-arg]
    )
    assert isinstance(s3, S3Store)
    assert s3.bucket == "b"
