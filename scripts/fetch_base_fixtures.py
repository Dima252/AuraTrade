"""Record the daily-bar fixtures for the base-count spot-check (task 2.2).

Free Alpaca market-data calls only (SIP daily bars, split-adjusted, 2016-2024); no orders.
Writes one gzipped CSV per ticker to `tests/fixtures/bases/` of the repository this script
lives in, whichever folder it is run from, so it can run against another checkout's `.env`:

    uv run python scripts/fetch_base_fixtures.py
    uv run python path/to/AuraTrade/scripts/fetch_base_fixtures.py --env-file .env
"""

from __future__ import annotations

import argparse
import gzip
import io
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values

BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
TICKERS = ("NVDA", "AMD", "ANET", "SHOP", "ENPH", "TSLA", "ZM", "CELH", "SMCI", "NFLX")
START, END = "2016-01-01", "2024-12-31"
OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "bases"
SOURCE = "Alpaca v2 stocks/bars timeframe=1Day feed=sip adjustment=split"


def fetch(http: httpx.Client, symbol: str) -> list[dict[str, Any]]:
    bars: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        params: dict[str, Any] = {
            "symbols": symbol,
            "timeframe": "1Day",
            "start": START,
            "end": END,
            "feed": "sip",
            "adjustment": "split",
            "limit": 10000,
        }
        if token:
            params["page_token"] = token
        response = http.get(BARS_URL, params=params)
        if response.status_code != 200:
            raise RuntimeError(f"{symbol}: HTTP {response.status_code}")
        body = response.json()
        bars.extend((body.get("bars") or {}).get(symbol, []))
        token = body.get("next_page_token")
        if not token:
            return bars


def to_csv(symbol: str, bars: list[dict[str, Any]], fetched: str) -> bytes:
    lines = [
        f"# symbol: {symbol}",
        f"# source: {SOURCE}",
        f"# fetched: {fetched}",
        "date,open,high,low,close,volume",
    ]
    for b in bars:
        if not b["v"]:
            continue  # zero-volume bars are dropped, as at ingest (#111)
        # Daily bars are stamped at midnight New York time, so the UTC date is the session.
        lines.append(
            f"{b['t'][:10]},{b['o']:.4f},{b['h']:.4f},{b['l']:.4f},{b['c']:.4f},{int(b['v'])}"
        )
    buffer = io.BytesIO()
    # mtime=0 keeps the file byte-identical across re-fetches of the same data.
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as gz:
        gz.write(("\n".join(lines) + "\n").encode())
    return buffer.getvalue()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args(argv)

    env = dotenv_values(args.env_file) if args.env_file.is_file() else {}
    key, secret = env.get("ALPACA_API_KEY"), env.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        print(f"ALPACA_API_KEY / ALPACA_SECRET_KEY missing in {args.env_file}", file=sys.stderr)
        return 2

    OUT.mkdir(parents=True, exist_ok=True)
    fetched = datetime.now(UTC).date().isoformat()
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    with httpx.Client(headers=headers, timeout=30) as http:
        for symbol in TICKERS:
            bars = fetch(http, symbol)
            path = OUT / f"{symbol}.csv.gz"
            path.write_bytes(to_csv(symbol, bars, fetched))
            first, last = (bars[0]["t"][:10], bars[-1]["t"][:10]) if bars else ("-", "-")
            print(f"{symbol:<5} {len(bars):>5} bars  {first} .. {last}  {path.stat().st_size:>7} B")
    return 0


if __name__ == "__main__":
    sys.exit(main())
