"""`scripts/preview_site.py`: the site and its snapshot, served locally under the deployed CSP."""

from __future__ import annotations

import functools
import threading
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

pytest.importorskip("aws_cdk")

from stacks.public_site import CONTENT_SECURITY_POLICY, WEB_DIR

from scripts.preview_site import handler


def serve(snapshot: Path | None) -> Iterator[str]:
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(handler(snapshot), directory=str(WEB_DIR))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def snapshot(tmp_path: Path) -> Path:
    path = tmp_path / "snapshot.json"
    path.write_text('{"schema": 1}', encoding="utf-8")
    return path


@pytest.fixture
def site(snapshot: Path) -> Iterator[str]:
    yield from serve(snapshot)


def test_pages_carry_the_deployed_policy(site: str) -> None:
    res = httpx.get(f"{site}/")
    assert res.headers["Content-Security-Policy"] == CONTENT_SECURITY_POLICY
    assert b"dashboard.js" in res.content


def test_the_snapshot_is_served_from_its_file(site: str) -> None:
    res = httpx.get(f"{site}/data/snapshot.json?v=1")
    assert res.headers["Content-Type"] == "application/json"
    assert res.content == b'{"schema": 1}'


@pytest.mark.parametrize("missing", [True, False])
def test_no_snapshot_is_a_404(tmp_path: Path, missing: bool) -> None:
    for base in serve(tmp_path / "none.json" if missing else None):
        assert httpx.get(f"{base}/data/snapshot.json").status_code == 404
