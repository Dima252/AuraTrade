"""Alpaca news (Benzinga), free: up to 50 items a page, history back to 2015 (spike 0.3a,
docs/spikes/news.md). It shares the market-data rate limit, so it takes the same
`VendorHttp` as `AlpacaDataClient` (`alpaca_http`).

Headlines and summaries are third-party text: callers pass them to the LLM only as
delimited data (README §9).
"""

from __future__ import annotations

import html
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from auratrade.data.alpaca_data import DATA_URL, parse_timestamp, rfc3339
from auratrade.data.http import VendorError, VendorHttp

PAGE_LIMIT = 50
# The `symbols` filter is a comma-separated query value; keep it to a modest length.
MAX_SYMBOLS = 100


@dataclass(frozen=True)
class NewsItem:
    id: int
    headline: str
    summary: str
    source: str
    url: str
    symbols: tuple[str, ...]
    created_at: datetime  # UTC
    updated_at: datetime  # UTC
    content: str | None  # HTML body, only when requested


class AlpacaNewsClient:
    def __init__(self, http: VendorHttp) -> None:
        self._http = http

    def news(
        self,
        symbols: Iterable[str],
        start: datetime,
        end: datetime,
        *,
        include_content: bool = False,
    ) -> list[NewsItem]:
        """Items created in [start, end] mentioning any of `symbols`, oldest first."""
        wanted = sorted(set(symbols))
        out: dict[int, NewsItem] = {}
        for i in range(0, len(wanted), MAX_SYMBOLS):
            token: str | None = None
            while True:
                body = self._http.get_json(
                    f"{DATA_URL}/v1beta1/news",
                    {
                        "symbols": ",".join(wanted[i : i + MAX_SYMBOLS]),
                        "start": rfc3339(start),
                        "end": rfc3339(end),
                        "limit": PAGE_LIMIT,
                        "sort": "asc",
                        "include_content": include_content,
                        "page_token": token,
                    },
                )
                if not isinstance(body, dict):
                    raise VendorError(
                        "alpaca", "/v1beta1/news: unexpected response", retryable=False
                    )
                for raw in body.get("news") or []:
                    item = _item(raw, include_content)
                    out[item.id] = item  # an item mentioning symbols in two chunks comes twice
                token = body.get("next_page_token")
                if not token:
                    break
        return sorted(out.values(), key=lambda n: (n.created_at, n.id))


def _item(raw: dict[str, Any], include_content: bool) -> NewsItem:
    content = raw.get("content") if include_content else None
    return NewsItem(
        int(raw["id"]),
        html.unescape(raw.get("headline") or ""),
        html.unescape(raw.get("summary") or ""),
        raw.get("source") or "",
        raw.get("url") or "",
        tuple(raw.get("symbols") or ()),
        parse_timestamp(raw["created_at"]),
        parse_timestamp(raw["updated_at"]),
        content or None,
    )
