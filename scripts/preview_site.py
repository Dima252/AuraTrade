"""Serve the public site locally, with its dashboard, under the deployed content-security policy.

    uv run python scripts/preview_site.py                          # the local storage's snapshot
    uv run python scripts/preview_site.py --snapshot path/to.json  # any snapshot file

`web/` is served as CloudFront serves it, and ``data/snapshot.json`` from the snapshot file
(`simulate` writes ``public/snapshot.json`` in ``STORAGE_LOCAL_DIR`` when run locally). Without
one, the page shows its before-the-trial text. Local only: it binds to 127.0.0.1.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from auratrade.config import REPO_ROOT, load_settings
from auratrade.journal.dashboard import KEY

sys.path.insert(0, str(REPO_ROOT / "infra"))
from stacks.public_site import CONTENT_SECURITY_POLICY, WEB_DIR

SNAPSHOT_PATH = "/data/snapshot.json"


def handler(snapshot: Path | None) -> type[SimpleHTTPRequestHandler]:
    class Handler(SimpleHTTPRequestHandler):
        def end_headers(self) -> None:
            self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def do_GET(self) -> None:
            if self.path.split("?")[0] != SNAPSHOT_PATH:
                super().do_GET()
                return
            if snapshot is None or not snapshot.is_file():
                self.send_error(404)
                return
            data = snapshot.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--snapshot", type=Path, help="the snapshot to serve (default: storage's)")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    snapshot = args.snapshot or load_settings().storage_local_dir / KEY
    if not snapshot.is_file():
        print(f"no snapshot at {snapshot}: the page shows its before-the-trial text")
    server = ThreadingHTTPServer(
        ("127.0.0.1", args.port),
        functools.partial(handler(snapshot), directory=str(WEB_DIR)),
    )
    print(f"serving {WEB_DIR} on http://127.0.0.1:{args.port}/ (Ctrl+C to stop)")
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
