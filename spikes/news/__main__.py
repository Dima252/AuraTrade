"""Spike 0.3f: the Alpaca news API on the free plan.

    uv run python -m spikes.news

Measures access, payload shape, headline volume (market-wide and per symbol), history depth,
content availability and rate-limit headers. Findings: docs/spikes/news.md.
"""

from __future__ import annotations

import time
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from spikes._common import Env, Log

HERE = Path(__file__).resolve().parent
NEWS_URL = "https://data.alpaca.markets/v1beta1/news"
SYMBOLS = ("AAPL", "NVDA", "SMCI", "CELH", "DXJ", "F", "IONQ", "APP", "SOFI", "BROS")
DAYS = (date(2026, 10, 7), date(2026, 10, 1), date(2025, 3, 12), date(2021, 6, 9), date(2016, 6, 8))


def fetch_all(
    http: httpx.Client, log: Log, params: dict[str, Any], max_pages: int = 40
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    token: str | None = None
    for _ in range(max_pages):
        page = dict(params, limit=50, **({"page_token": token} if token else {}))
        response = http.get(NEWS_URL, params=page)
        if response.status_code != 200:
            log.event("http_error", params=page, status=response.status_code, body=response.text)
            break
        body = response.json()
        items.extend(body.get("news", []))
        token = body.get("next_page_token")
        if not token:
            break
    return items


def day_bounds(d: date) -> dict[str, str]:
    start = datetime(d.year, d.month, d.day, tzinfo=UTC)
    return {"start": start.isoformat(), "end": (start + timedelta(days=1)).isoformat()}


def main() -> None:
    env = Env()
    log = Log(HERE, "news", env.secrets())
    with httpx.Client(headers=env.headers(), timeout=30) as http:
        response = http.get(NEWS_URL, params={"limit": 5, "include_content": "true"})
        log.event(
            "access",
            status=response.status_code,
            rate_limit={k: v for k, v in response.headers.items() if "ratelimit" in k.lower()},
        )
        latest = response.json().get("news", [])
        if latest:
            sample = dict(latest[0])
            sample["content"] = (sample.get("content") or "")[:300]
            log.event(
                "payload_shape",
                fields=sorted(latest[0]),
                sample=sample,
                lag_min=(
                    datetime.now(UTC) - datetime.fromisoformat(latest[0]["created_at"])
                ).total_seconds()
                / 60,
                content_lengths=[len(n.get("content") or "") for n in latest],
            )

        for d in DAYS:
            t0 = time.monotonic()
            items = fetch_all(http, log, day_bounds(d))
            log.event(
                "market_wide_day",
                day=d,
                articles=len(items),
                seconds=round(time.monotonic() - t0, 1),
                sources=Counter(n.get("source") for n in items).most_common(5),
                with_symbols=sum(1 for n in items if n.get("symbols")),
                median_symbols=sorted(len(n.get("symbols") or []) for n in items)[len(items) // 2]
                if items
                else 0,
            )

        since = datetime.now(UTC) - timedelta(days=30)
        for sym in SYMBOLS:
            items = fetch_all(http, log, {"symbols": sym, "start": since.isoformat()})
            log.event(
                "per_symbol_30d",
                symbol=sym,
                articles=len(items),
                headlines=[n["headline"][:100] for n in items[:3]],
            )

        oldest = http.get(
            NEWS_URL, params={"limit": 3, "sort": "asc", "start": "2010-01-01T00:00:00Z"}
        )
        log.event(
            "oldest_articles",
            status=oldest.status_code,
            items=[(n["created_at"], n["headline"][:80]) for n in oldest.json().get("news", [])],
        )


if __name__ == "__main__":
    main()
