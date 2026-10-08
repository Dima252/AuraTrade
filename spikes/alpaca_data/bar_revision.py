"""Does a daily bar fetched at ~17:00 ET change later (after-hours volume)?

uv run python -m spikes.alpaca_data.bar_revision snapshot   # ~17:00 ET on session T
uv run python -m spikes.alpaca_data.bar_revision compare    # T+1 or later
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta

import httpx

from spikes._common import Env

from .__main__ import DATA, OUT, chunks, load_universe

SNAPSHOT = OUT / "bar_revision.json"


def fetch(env: Env, symbols: list[str], day: str, end: str) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for chunk in chunks(symbols, 200):
        r = httpx.get(
            f"{DATA}/v2/stocks/bars",
            params={
                "symbols": ",".join(chunk),
                "timeframe": "1Day",
                "start": day,
                "end": end,
                "feed": "sip",
                "adjustment": "split",
                "limit": 10000,
            },
            headers=env.headers(),
            timeout=60,
        )
        r.raise_for_status()
        for sym, rows in r.json()["bars"].items():
            out[sym] = rows[0]
    return out


def main() -> None:
    env = Env()
    universe = load_universe()["common"]
    symbols = universe[:: max(1, len(universe) // 200)][:200]
    if sys.argv[1] == "snapshot":
        now = datetime.now(UTC)
        day = now.date().isoformat()
        bars = fetch(env, symbols, day, (now - timedelta(minutes=16)).isoformat())
        SNAPSHOT.write_text(json.dumps({"day": day, "fetched": now.isoformat(), "bars": bars}))
        print(f"snapshot of {len(bars)} bars for {day} at {now.isoformat()}")
        return
    snap = json.loads(SNAPSHOT.read_text())
    later = fetch(env, list(snap["bars"]), snap["day"], snap["day"] + "T23:59:00Z")
    changed = dict.fromkeys(("o", "h", "l", "c", "v", "n", "vw"), 0)
    vol_ratio = []
    for sym, early in snap["bars"].items():
        if sym not in later:
            continue
        for f in changed:
            if early[f] != later[sym][f]:
                changed[f] += 1
        if early["v"]:
            vol_ratio.append(later[sym]["v"] / early["v"])
    vol_ratio.sort()
    print(
        json.dumps(
            {
                "day": snap["day"],
                "fetched": snap["fetched"],
                "compared": len(vol_ratio),
                "fields_changed": changed,
                "volume_ratio_median": vol_ratio[len(vol_ratio) // 2],
                "volume_ratio_p90": vol_ratio[int(0.9 * len(vol_ratio))],
                "volume_ratio_max": vol_ratio[-1],
            }
        )
    )


if __name__ == "__main__":
    main()
