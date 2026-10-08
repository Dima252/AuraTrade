"""Spike 0.3h, step 1: daily bars 2016-2022 into local Parquet (spikes/edge_check/data/).

    uv run --group spikes python -m spikes.edge_check.fetch

Symbols: today's common stocks + Alpaca's inactive listed symbols + every 1-4 letter ticker that
traded in the first week of 2016 or of June 2017 (spike 0.3a's enumeration; Alpaca's asset list
forgets most pre-2018 delistings). ETFs from the Nasdaq Trader directory are dropped.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

from spikes._common import Env
from spikes.alpaca_data.__main__ import OUT as DATA_SPIKE_OUT
from spikes.alpaca_data.__main__ import VALID_SYMBOL

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
START, END = "2016-01-01", "2022-12-31"
CHUNK = 50
WORKERS = 12
PER_MINUTE = 180


class Limiter:
    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self.sent: deque[float] = deque()
        self.lock = threading.Lock()

    def wait(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                while self.sent and now - self.sent[0] > 60:
                    self.sent.popleft()
                if len(self.sent) < self.per_minute:
                    self.sent.append(now)
                    return
                delay = 60 - (now - self.sent[0]) + 0.05
            time.sleep(delay)


def candidate_symbols() -> tuple[list[str], dict[str, str]]:
    universe = json.loads((DATA_SPIKE_OUT / "universe.json").read_text(encoding="utf-8"))
    source: dict[str, str] = {}
    for s in universe["common"]:
        source[s] = "active"
    for s in universe["inactive_listed"]:
        s = s.removesuffix("_DELISTED")
        if VALID_SYMBOL.fullmatch(s):
            source.setdefault(s, "inactive")
    for name in ("enumerated_2016-01-04.json", "enumerated_2017-06-05.json"):
        path = DATA_SPIKE_OUT / name
        if path.exists():
            for s in json.loads(path.read_text(encoding="utf-8")):
                source.setdefault(s, "enumerated")
    # Active non-common listings (ETFs, funds, SPAC shells...) were already excluded from
    # "common"; drop them here too so the enumeration doesn't bring them back.
    status = universe["status"]
    known_active_non_common = {
        s for s, st in status.items() if st == "active" and s not in set(universe["common"])
    }
    keep = sorted(s for s in source if s not in known_active_non_common)
    source["SPY"] = "benchmark"  # session calendar and the market-trend breakdown only
    return [*keep, "SPY"], source


def fetch_chunk(
    http: httpx.Client, limiter: Limiter, symbols: list[str], adjustment: str
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        params: dict[str, Any] = {
            "symbols": ",".join(symbols),
            "timeframe": "1Day",
            "start": START,
            "end": END,
            "feed": "sip",
            "adjustment": adjustment,
            "limit": 10000,
        }
        if token:
            params["page_token"] = token
        for attempt in range(5):
            limiter.wait()
            try:
                response = http.get(BARS_URL, params=params)
            except httpx.HTTPError:
                time.sleep(2**attempt)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                time.sleep(2**attempt)
                continue
            break
        if response.status_code != 200:
            print(f"chunk {symbols[0]}..: HTTP {response.status_code} {response.text[:120]}")
            return rows
        body = response.json()
        for sym, bars in (body.get("bars") or {}).items():
            for b in bars:
                rows.append(
                    {
                        "symbol": sym,
                        "date": b["t"][:10],
                        "o": b["o"],
                        "h": b["h"],
                        "l": b["l"],
                        "c": b["c"],
                        "v": b["v"],
                    }
                )
        token = body.get("next_page_token")
        if not token:
            return rows


def main() -> None:
    env = Env()
    DATA.mkdir(parents=True, exist_ok=True)
    symbols, source = candidate_symbols()
    (DATA / "sources.json").write_text(json.dumps(source), encoding="utf-8")
    print(
        f"{len(symbols)} candidate symbols: "
        + json.dumps(
            {
                k: sum(1 for s in symbols if source[s] == k)
                for k in ("active", "inactive", "enumerated")
            }
        )
    )
    limiter = Limiter(PER_MINUTE)
    chunks = [symbols[i : i + CHUNK] for i in range(0, len(symbols), CHUNK)]
    with httpx.Client(headers=env.headers(), timeout=120) as http:
        for adjustment in ("split", "raw"):
            path = DATA / f"bars_{adjustment}.parquet"
            if path.exists():
                print(f"{path.name} exists, skipping")
                continue
            t0 = time.monotonic()
            done = 0
            frames: list[pd.DataFrame] = []

            def job(chunk: list[str], adj: str = adjustment) -> list[dict[str, Any]]:
                return fetch_chunk(http, limiter, chunk, adj)

            with ThreadPoolExecutor(WORKERS) as pool:
                for rows in pool.map(job, chunks):
                    done += 1
                    if rows:
                        frames.append(pd.DataFrame(rows))
                    if done % 20 == 0:
                        print(
                            f"{adjustment}: {done}/{len(chunks)} chunks, "
                            f"{time.monotonic() - t0:.0f}s",
                            flush=True,
                        )
            df = pd.concat(frames, ignore_index=True)
            if adjustment == "raw":
                df = df[["symbol", "date", "c"]].rename(columns={"c": "c_raw"})
            df.to_parquet(path, index=False)
            print(
                f"{path.name}: {len(df):,} bars, {df.symbol.nunique():,} symbols, "
                f"{time.monotonic() - t0:.0f}s",
                flush=True,
            )


if __name__ == "__main__":
    main()
