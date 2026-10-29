"""The `dashboard` tool's page, an MCP App (README §5.10, task 6.6, #176).

`dashboard.html` with the site's drawing inlined (`web/chart.css`, `web/chart.js`): the host
renders it in a sandboxed frame whose default policy loads and fetches nothing, so the page is
one self-contained document and its data comes in the tool's result.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

from auratrade.mcp_server.docs import DOCS_ROOT

URI = "ui://auratrade/dashboard.html"
PAGE = Path(__file__).with_name("dashboard.html")
# The remote server's image keeps the two files under its documents' root, as in the repository.
WEB = DOCS_ROOT / "web"
MARKERS = {"/*@chart.css*/": "chart.css", "/*@chart.js*/": "chart.js"}


@cache
def html() -> str:
    page = PAGE.read_text(encoding="utf-8")
    for marker, name in MARKERS.items():
        head, found, tail = page.partition(marker)
        if not found:
            raise ValueError(f"dashboard.html has no {marker} marker")
        code = (WEB / name).read_text(encoding="utf-8")
        if "</script" in code.lower() or "</style" in code.lower():
            raise ValueError(f"{name} would close its own element")
        page = head + code + tail
    return page
