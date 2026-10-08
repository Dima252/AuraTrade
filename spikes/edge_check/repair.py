"""Re-fetch chunks that fetch.py logged as failed (HTTP 429 under contention) and patch the Parquet.

uv run --group spikes python -m spikes.edge_check.repair
"""

from __future__ import annotations

import re

import httpx
import pandas as pd

from spikes._common import Env

from .fetch import CHUNK, DATA, HERE, Limiter, candidate_symbols, fetch_chunk


def main() -> None:
    log = (HERE / "fetch.out").read_text(encoding="utf-8")
    symbols, _ = candidate_symbols()
    chunks = {c[0]: c for c in (symbols[i : i + CHUNK] for i in range(0, len(symbols), CHUNK))}
    sections = re.split(r"(?m)^(?=raw: )", log, maxsplit=1)
    failed = {
        "split": re.findall(r"chunk (\S+?)\.\.: HTTP", sections[0]),
        "raw": re.findall(r"chunk (\S+?)\.\.: HTTP", sections[1]) if len(sections) > 1 else [],
    }
    print("failed chunks:", failed)
    limiter = Limiter(150)
    with httpx.Client(headers=Env().headers(), timeout=120) as http:
        for adjustment, starts in failed.items():
            if not starts:
                continue
            path = DATA / f"bars_{adjustment}.parquet"
            df = pd.read_parquet(path)
            for start in starts:
                chunk = chunks[start]
                rows = fetch_chunk(http, limiter, chunk, adjustment)
                new = pd.DataFrame(rows)
                if adjustment == "raw":
                    new = new[["symbol", "date", "c"]].rename(columns={"c": "c_raw"})
                df = pd.concat([df[~df.symbol.isin(chunk)], new], ignore_index=True)
                print(
                    f"{adjustment} {start}..: {len(new):,} bars for {new.symbol.nunique()} symbols"
                )
            df.to_parquet(path, index=False)


if __name__ == "__main__":
    main()
