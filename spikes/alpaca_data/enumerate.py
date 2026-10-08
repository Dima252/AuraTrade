"""Survivorship workaround probe: find every 1-4 letter ticker with traded bars in one week.

Alpaca's asset list forgets most tickers delisted before ~2018, but the bars endpoint still
serves them. Asking for every possible 1-4 letter ticker recovers the symbols that traded.

    uv run python -m spikes.alpaca_data.enumerate 2016-01-04 2016-01-08
"""

from __future__ import annotations

import itertools
import json
import string
import sys
import time

from spikes._common import Env, Log

from .__main__ import HERE, OUT, Http, load_universe


def main() -> None:
    start, end = sys.argv[1], sys.argv[2]
    env = Env()
    log = Log(HERE, f"enumerate_{start}", env.secrets())
    http = Http(env, log)
    letters = string.ascii_uppercase
    candidates = ["".join(p) for n in range(1, 5) for p in itertools.product(letters, repeat=n)]
    found: dict[str, dict[str, float]] = {}
    t0 = time.monotonic()
    for i in range(0, len(candidates), 1000):
        got = http.bars(candidates[i : i + 1000], "1Day", start, end, feed="sip", adjustment="raw")
        for sym, bars in got.items():
            traded = [b for b in bars if b["v"] > 0]
            if traded:
                found[sym] = {"c": traded[-1]["c"], "v": traded[-1]["v"]}
    known = set(load_universe()["all_symbols"])
    known |= {s.removesuffix("_DELISTED") for s in known}
    liquid = {s for s, b in found.items() if b["c"] >= 10 and b["c"] * b["v"] >= 10_000_000}
    log.event(
        "enumerated",
        window=(start, end),
        candidates=len(candidates),
        requests=http.requests,
        seconds=round(time.monotonic() - t0),
        traded=len(found),
        not_in_asset_list=len(set(found) - known),
        liquid_day=len(liquid),
        liquid_not_in_asset_list=len(liquid - known),
        sample_missing_liquid=sorted(liquid - known)[:60],
    )
    (OUT / f"enumerated_{start}.json").write_text(json.dumps(found))


if __name__ == "__main__":
    main()
