"""Spike 0.3a: Alpaca market data on the free plan.

    uv run --group spikes python -m spikes.alpaca_data <scenario>

    assets        universe + classification (run first: writes output/universe.json)
    daily         SIP daily bars: depth, speed, regular session or not
    adjustments   raw vs split closes, corporate actions, ticker changes
    intraday      IEX vs SIP opening-range high and 15:40 price
    iex_depth     how far back IEX minute bars go
    premarket     IEX pre-market gap estimates vs the actual open
    survivorship  delisted symbols' history
    realtime      market hours only: IEX freshness, SIP delay

Findings: docs/spikes/alpaca-data.md.
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import statistics
import time
from collections import Counter, deque
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from spikes._common import Env, Log

HERE = Path(__file__).resolve().parent
OUT = HERE / "output"
UNIVERSE = OUT / "universe.json"
ET = ZoneInfo("America/New_York")
DATA = "https://data.alpaca.markets"
TRADING = "https://paper-api.alpaca.markets"
NASDAQ_DIR = "https://www.nasdaqtrader.com/dynamic/SymDir/{}.txt"
LISTED = ("NYSE", "NASDAQ", "AMEX", "ARCA", "BATS")
NOT_COMMON = re.compile(
    r"\b(ETF|ETN|Fund|Trust|Units?|Warrants?|Rights?|Preferred|Pfd|Depositary Shares"
    r"|Notes? due|Debentures|Exchange[- ]Traded|Index|Shares of Beneficial Interest)\b",
    re.I,
)
SPAC = re.compile(r"\b(Acquisition|Merger|Capital) Corp", re.I)
SYMBOL_SUFFIX = re.compile(r"[./-]")
VALID_SYMBOL = re.compile(r"[A-Z]{1,5}(\.[A-Z]{1,2})?")
# Well-known delistings 2017-2022 (acquisitions, bankruptcies, ticker changes): the sample
# that Alpaca's survivorship coverage is checked against. Year = the year trading ended.
KNOWN_DELISTED: dict[str, tuple[int, str]] = {
    "WFM": (2017, "acquired"),
    "LLTC": (2017, "acquired"),
    "RAI": (2017, "acquired"),
    "MJN": (2017, "acquired"),
    "SPLS": (2017, "acquired"),
    "PNRA": (2017, "acquired"),
    "KATE": (2017, "acquired"),
    "CAB": (2017, "acquired"),
    "STJ": (2017, "acquired"),
    "BRCD": (2017, "acquired"),
    "LVLT": (2017, "acquired"),
    "YHOO": (2017, "ticker change (AABA)"),
    "TWX": (2018, "acquired"),
    "MON": (2018, "acquired"),
    "AET": (2018, "acquired"),
    "ESRX": (2018, "acquired"),
    "COL": (2018, "acquired"),
    "XL": (2018, "acquired"),
    "CA": (2018, "acquired"),
    "EVHC": (2018, "acquired"),
    "DPS": (2018, "merger, ticker change (KDP)"),
    "KORS": (2018, "ticker change (CPRI)"),
    "SHLD": (2018, "bankruptcy"),
    "CELG": (2019, "acquired"),
    "RHT": (2019, "acquired"),
    "APC": (2019, "acquired"),
    "TSS": (2019, "acquired"),
    "ARRS": (2019, "acquired"),
    "STI": (2019, "merger (TFC)"),
    "BBT": (2019, "ticker change (TFC)"),
    "CBS": (2019, "merger (VIAC)"),
    "AABA": (2019, "liquidation"),
    "WCG": (2020, "acquired"),
    "AGN": (2020, "acquired"),
    "ETFC": (2020, "acquired"),
    "JCP": (2020, "bankruptcy"),
    "HTZ": (2020, "bankruptcy (relisted 2021)"),
    "CHK": (2020, "bankruptcy (relisted 2021)"),
    "WLL": (2020, "bankruptcy (relisted)"),
    "GNC": (2020, "bankruptcy"),
    "LK": (2020, "fraud delisting"),
    "TIF": (2021, "acquired"),
    "XLNX": (2022, "acquired"),
    "CERN": (2022, "acquired"),
    "CTXS": (2022, "acquired"),
    "TWTR": (2022, "acquired"),
    "FB": (2022, "ticker change (META)"),
    "ANTM": (2022, "ticker change (ELV)"),
}


class Http:
    """httpx with a sliding-window limiter under the 200 req/min free-plan limit."""

    def __init__(self, env: Env, log: Log, per_minute: int = 190) -> None:
        self.client = httpx.Client(headers=env.headers(), timeout=60)
        self.log = log
        self.per_minute = per_minute
        self.sent: deque[float] = deque()
        self.requests = 0

    def get(self, url: str, params: dict[str, Any]) -> httpx.Response:
        while True:
            now = time.monotonic()
            while self.sent and now - self.sent[0] > 60:
                self.sent.popleft()
            if len(self.sent) >= self.per_minute:
                time.sleep(60 - (now - self.sent[0]) + 0.05)
                continue
            self.sent.append(now)
            self.requests += 1
            response = self.client.get(url, params=params)
            if response.status_code == 429:
                self.log.event("rate_limited", headers=dict(response.headers))
                time.sleep(5)
                continue
            return response

    def bars(
        self, symbols: list[str], timeframe: str, start: str, end: str, **extra: str
    ) -> dict[str, list[dict[str, Any]]]:
        """All pages of a multi-symbol bars request."""
        out: dict[str, list[dict[str, Any]]] = {}
        token: str | None = None
        while True:
            params: dict[str, Any] = {
                "symbols": ",".join(symbols),
                "timeframe": timeframe,
                "start": start,
                "end": end,
                "limit": 10000,
                **extra,
            }
            if token:
                params["page_token"] = token
            response = self.get(f"{DATA}/v2/stocks/bars", params)
            if response.status_code != 200:
                self.log.event(
                    "bars_error",
                    status=response.status_code,
                    body=response.text[:500],
                    params={k: v for k, v in params.items() if k != "symbols"},
                    n_symbols=len(symbols),
                )
                return out
            body = response.json()
            for sym, rows in (body.get("bars") or {}).items():
                out.setdefault(sym, []).extend(rows)
            token = body.get("next_page_token")
            if not token:
                return out


def chunks(items: list[str], size: int) -> Iterator[list[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def et(day: date, hh: int, mm: int) -> str:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=ET).isoformat()


def bar_et(row: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(row["t"].replace("Z", "+00:00")).astimezone(ET)


def pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def summary(values: list[float]) -> dict[str, float | int]:
    return {
        "n": len(values),
        "median": round(statistics.median(values), 5) if values else float("nan"),
        "p10": round(pct(values, 0.10), 5),
        "p90": round(pct(values, 0.90), 5),
        "max": round(max(values), 5) if values else float("nan"),
    }


def sessions(http: Http, start: date, end: date) -> list[date]:
    response = http.get(
        f"{TRADING}/v2/calendar", {"start": start.isoformat(), "end": end.isoformat()}
    )
    return [date.fromisoformat(r["date"]) for r in response.json()]


def load_universe() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(UNIVERSE.read_text(encoding="utf-8"))
    return data


def spike(name: str) -> tuple[Env, Log, Http]:
    env = Env()
    log = Log(HERE, name, env.secrets())
    return env, log, Http(env, log)


# --- assets ------------------------------------------------------------------------------


def nasdaq_directory(log: Log) -> dict[str, dict[str, str]]:
    """Nasdaq Trader's public symbol directory (no key): it carries an ETF flag."""
    out: dict[str, dict[str, str]] = {}
    for name, sym_col in (("nasdaqlisted", "Symbol"), ("otherlisted", "ACT Symbol")):
        response = httpx.get(NASDAQ_DIR.format(name), timeout=60)
        if response.status_code != 200:
            log.event("nasdaq_dir_error", file=name, status=response.status_code)
            continue
        lines = response.text.strip().splitlines()
        header = lines[0].split("|")
        for line in lines[1:]:
            if line.startswith("File Creation Time"):
                continue
            row = dict(zip(header, line.split("|"), strict=False))
            sym = row[sym_col].replace("$", ".PR").replace("=", ".U")
            out[sym] = row
    return out


def scenario_assets(args: argparse.Namespace) -> None:
    _, log, http = spike("assets")
    assets = http.get(f"{TRADING}/v2/assets", {"asset_class": "us_equity"}).json()
    log.event("asset_fields", fields=sorted(assets[0]), total=len(assets))
    log.event("by_status_exchange", counts=Counter((a["status"], a["exchange"]) for a in assets))
    directory = nasdaq_directory(log)
    active = [
        a for a in assets if a["status"] == "active" and a["tradable"] and a["exchange"] in LISTED
    ]

    def kind(a: dict[str, Any]) -> str:
        name, sym = a["name"] or "", a["symbol"]
        row = directory.get(sym)
        if row is not None and row.get("ETF") == "Y":
            return "etf (directory)"
        if SYMBOL_SUFFIX.search(sym):
            return "suffix (pref/unit/warrant/class)"
        if SPAC.search(name):
            return "spac-like name"
        if NOT_COMMON.search(name):
            return "non-common name"
        return "common"

    kinds = Counter(kind(a) for a in active)
    by_exchange = Counter((a["exchange"], kind(a)) for a in active)
    name_etf = {a["symbol"] for a in active if re.search(r"\bETF\b|\bETN\b", a["name"] or "")}
    dir_etf = {a["symbol"] for a in active if directory.get(a["symbol"], {}).get("ETF") == "Y"}
    log.event(
        "classification",
        active_tradable_listed=len(active),
        kinds=kinds,
        by_exchange=by_exchange,
        in_directory=sum(1 for a in active if a["symbol"] in directory),
        etf_by_name=len(name_etf),
        etf_by_directory=len(dir_etf),
        etf_name_not_directory=sorted(name_etf - dir_etf)[:30],
        etf_directory_not_name=len(dir_etf - name_etf),
        etf_directory_not_name_sample=[
            (s, next(a["name"] for a in active if a["symbol"] == s))
            for s in sorted(dir_etf - name_etf)[:15]
        ],
    )
    common = [a for a in active if kind(a) == "common"]
    spacs = [(a["symbol"], a["name"]) for a in active if kind(a) == "spac-like name"]
    log.event("spac_like_sample", count=len(spacs), sample=spacs[:25])
    log.event(
        "common_name_suffixes",
        counts=Counter(" ".join((a["name"] or "").split()[-2:]) for a in common).most_common(25),
    )
    inactive_listed = [
        a["symbol"] for a in assets if a["status"] == "inactive" and a["exchange"] in LISTED
    ]
    OUT.mkdir(parents=True, exist_ok=True)
    UNIVERSE.write_text(
        json.dumps(
            {
                "common": sorted(a["symbol"] for a in common),
                "inactive_listed": sorted(inactive_listed),
                "all_symbols": sorted({a["symbol"] for a in assets}),
                "status": {a["symbol"]: a["status"] for a in assets},
            }
        ),
        encoding="utf-8",
    )
    log.event("universe_written", common=len(common), inactive_listed=len(inactive_listed))


# --- daily bars --------------------------------------------------------------------------


def scenario_daily(args: argparse.Namespace) -> None:
    _, log, http = spike("daily")
    universe = load_universe()["common"]
    today = datetime.now(ET).date()
    days = sessions(http, today - timedelta(days=14), today)
    last = days[-1] if datetime.now(ET).hour >= 17 else days[-2]

    # 1. Depth: earliest SIP split-adjusted bar for long-listed names.
    deep = ["AAPL", "MSFT", "SPY", "F", "XOM", "JPM", "KO", "INTC", "GE", "T"]
    t0 = time.monotonic()
    bars = http.bars(deep, "1Day", "2010-01-01", last.isoformat(), feed="sip", adjustment="split")
    log.event(
        "depth",
        seconds=round(time.monotonic() - t0, 1),
        first={s: rows[0]["t"][:10] for s, rows in bars.items()},
        counts={s: len(rows) for s, rows in bars.items()},
    )

    # 2. One session for the whole universe: how long, how complete, best chunk size.
    for size in (100, 500, 1000):
        t0, n_req = time.monotonic(), http.requests
        got: dict[str, list[dict[str, Any]]] = {}
        for chunk in chunks(universe, size):
            got.update(
                http.bars(
                    chunk,
                    "1Day",
                    last.isoformat(),
                    last.isoformat(),
                    feed="sip",
                    adjustment="split",
                )
            )
        log.event(
            "one_session_universe",
            session=last,
            chunk=size,
            symbols=len(universe),
            with_bar=len(got),
            requests=http.requests - n_req,
            seconds=round(time.monotonic() - t0, 1),
        )

    # 3. Full-history throughput on a sample, to extrapolate the 2016+ backfill.
    sample = universe[:: max(1, len(universe) // 300)][:300]
    t0, n_req = time.monotonic(), http.requests
    hist: dict[str, list[dict[str, Any]]] = {}
    for chunk in chunks(sample, 100):
        hist.update(
            http.bars(chunk, "1Day", "2016-01-01", last.isoformat(), feed="sip", adjustment="split")
        )
    n_bars = sum(len(v) for v in hist.values())
    log.event(
        "history_throughput",
        symbols=len(sample),
        bars=n_bars,
        requests=http.requests - n_req,
        seconds=round(time.monotonic() - t0, 1),
        bars_per_request=round(n_bars / max(1, http.requests - n_req)),
    )

    # 4. Is the daily bar the regular session (09:30-16:00) or does it include extended hours?
    check = ["AAPL", "NVDA", "TSLA", "F", "AMD", "PLTR", "SOFI", "KO", "XOM", "JPM"]
    rows = []
    for day in days[-6:-1]:
        daily = http.bars(
            check, "1Day", day.isoformat(), day.isoformat(), feed="sip", adjustment="raw"
        )
        minute = http.bars(
            check, "1Min", et(day, 4, 0), et(day, 20, 0), feed="sip", adjustment="raw"
        )
        for sym in check:
            if sym not in daily or sym not in minute:
                continue
            d = daily[sym][0]
            mins = minute[sym]
            rth = [m for m in mins if (9, 30) <= (bar_et(m).hour, bar_et(m).minute) < (16, 0)]
            if not rth:
                continue
            rows.append(
                {
                    "symbol": sym,
                    "day": day,
                    "daily_open": d["o"],
                    "rth_first_open": rth[0]["o"],
                    "daily_high": d["h"],
                    "rth_high": max(m["h"] for m in rth),
                    "ext_high": max(m["h"] for m in mins),
                    "daily_low": d["l"],
                    "rth_low": min(m["l"] for m in rth),
                    "ext_low": min(m["l"] for m in mins),
                    "daily_close": d["c"],
                    "rth_last_close": rth[-1]["c"],
                    "daily_volume": d["v"],
                    "rth_volume": sum(m["v"] for m in rth),
                    "ext_volume": sum(m["v"] for m in mins),
                }
            )
    within_rth = sum(
        1
        for r in rows
        if r["daily_high"] <= r["rth_high"] + 1e-6 and r["daily_low"] >= r["rth_low"] - 1e-6
    )
    ext_differs = sum(
        1 for r in rows if r["ext_high"] > r["rth_high"] or r["ext_low"] < r["rth_low"]
    )
    log.event(
        "rth_check",
        rows=len(rows),
        daily_range_within_rth_minutes=within_rth,
        days_where_extended_hours_exceed_rth=ext_differs,
        volume_ratio_daily_over_rth=summary([r["daily_volume"] / r["rth_volume"] for r in rows]),
        volume_ratio_daily_over_ext=summary([r["daily_volume"] / r["ext_volume"] for r in rows]),
        open_diff=summary([abs(r["daily_open"] - r["rth_first_open"]) for r in rows]),
        close_diff=summary([abs(r["daily_close"] - r["rth_last_close"]) for r in rows]),
        sample=rows[:4],
    )

    # 5. Recent SIP on the free plan: refused within 15 minutes?
    now = datetime.now(UTC)
    response = http.get(
        f"{DATA}/v2/stocks/bars",
        {
            "symbols": "SPY",
            "timeframe": "1Min",
            "start": (now - timedelta(minutes=30)).isoformat(),
            "end": now.isoformat(),
            "feed": "sip",
        },
    )
    log.event("recent_sip", status=response.status_code, body=response.text[:300])


# --- adjustments and corporate actions ---------------------------------------------------


def scenario_adjustments(args: argparse.Namespace) -> None:
    _, log, http = spike("adjustments")
    # (symbol, last pre-split session, split ex-date, ratio)
    splits = [
        ("NVDA", "2024-06-07", "2024-06-10", 10),
        ("AAPL", "2020-08-28", "2020-08-31", 4),
        ("TSLA", "2022-08-24", "2022-08-25", 3),
        ("GOOGL", "2022-07-15", "2022-07-18", 20),
        ("GE", "2021-07-30", "2021-08-02", 1 / 8),
    ]
    rows = []
    for sym, before, ex, ratio in splits:
        out: dict[str, Any] = {"symbol": sym, "ratio": ratio}
        for adj in ("raw", "split", "all"):
            got = http.bars([sym], "1Day", before, ex, feed="sip", adjustment=adj).get(sym, [])
            out[adj] = [(r["t"][:10], r["c"]) for r in got]
        rows.append(out)
    log.event("split_adjustment", rows=rows)

    # The newer corporate-actions endpoint on the data API.
    for params in (
        {"symbols": "NVDA,AAPL,TSLA", "types": "forward_split", "start": "2020-01-01"},
        {"symbols": "GE", "types": "reverse_split", "start": "2021-01-01"},
        {"symbols": "FB,META", "types": "name_change", "start": "2022-01-01"},
        {"symbols": "ATVI,TWTR,XLNX", "start": "2022-01-01"},
        {"types": "cash_merger,stock_merger", "start": "2026-09-01"},
        {"symbols": "KO", "types": "cash_dividend", "start": "2026-01-01"},
        {"symbols": "AAPL", "start": "2016-01-01", "end": "2018-12-31"},
    ):
        response = http.get(f"{DATA}/v1/corporate-actions", dict(params, limit=50))
        body = response.json() if response.status_code == 200 else {}
        actions = body.get("corporate_actions", {})
        log.event(
            "corporate_actions",
            params=params,
            status=response.status_code,
            types={k: len(v) for k, v in actions.items()},
            sample={k: v[:2] for k, v in actions.items()},
            error=None if response.status_code == 200 else response.text[:300],
        )

    # Symbol changes: does the old ticker's history survive under either symbol?
    for old, new, start, end in (
        ("FB", "META", "2021-06-01", "2021-06-04"),
        ("SQ", "XYZ", "2024-06-03", "2024-06-05"),
        ("ANTM", "ELV", "2021-06-01", "2021-06-03"),
    ):
        got = http.bars([old, new], "1Day", start, end, feed="sip", adjustment="split")
        log.event(
            "symbol_change",
            old=old,
            new=new,
            window=(start, end),
            old_bars=[(r["t"][:10], r["c"]) for r in got.get(old, [])],
            new_bars=[(r["t"][:10], r["c"]) for r in got.get(new, [])],
        )


# --- intraday: IEX vs SIP ----------------------------------------------------------------


def liquid_sample(http: Http, log: Log, universe: list[str], day: date, n: int) -> list[str]:
    """n symbols spread across the liquid end of the universe (price >= $10, $ volume rank)."""
    got: dict[str, list[dict[str, Any]]] = {}
    for chunk in chunks(universe, 1000):
        got.update(
            http.bars(chunk, "1Day", day.isoformat(), day.isoformat(), feed="sip", adjustment="raw")
        )
    ranked = sorted(
        (s for s, rows in got.items() if rows[0]["c"] >= 10),
        key=lambda s: -got[s][0]["c"] * got[s][0]["v"],
    )[:1500]
    picked = ranked[:: max(1, len(ranked) // n)][:n]
    log.event("sample", day=day, liquid=len(ranked), picked=picked)
    return picked


def scenario_intraday(args: argparse.Namespace) -> None:
    _, log, http = spike("intraday")
    universe = load_universe()["common"]
    today = datetime.now(ET).date()
    days = sessions(http, today - timedelta(days=14), today)[-5:]
    sample = liquid_sample(http, log, universe, days[-2], 50)
    orh_gap, orh_missing, p1540_gap, vol_share, iex_minutes = [], 0, [], [], []
    rows = []
    for day in days:
        mins = {
            feed: http.bars(sample, "1Min", et(day, 9, 30), et(day, 16, 0), feed=feed)
            for feed in ("iex", "sip")
        }
        for sym in sample:
            iex, sip = mins["iex"].get(sym, []), mins["sip"].get(sym, [])
            if not sip:
                continue
            first5 = [(9, 30), (9, 31), (9, 32), (9, 33), (9, 34)]

            def in_or(m: dict[str, Any], keys: list[tuple[int, int]] = first5) -> bool:
                t = bar_et(m)
                return (t.hour, t.minute) in keys

            iex_or = [m for m in iex if in_or(m)]
            sip_or = [m for m in sip if in_or(m)]
            iex_minutes.append(len(iex) / max(1, len(sip)))
            vol_share.append(sum(m["v"] for m in iex) / max(1, sum(m["v"] for m in sip)))
            if not iex_or:
                orh_missing += 1
            elif sip_or:
                sip_h, iex_h = max(m["h"] for m in sip_or), max(m["h"] for m in iex_or)
                orh_gap.append((sip_h - iex_h) / sip_h)
                rows.append({"symbol": sym, "day": day, "sip_orh": sip_h, "iex_orh": iex_h})

            def price_at(bars: list[dict[str, Any]]) -> float | None:
                before = [m for m in bars if (bar_et(m).hour, bar_et(m).minute) < (15, 40)]
                return before[-1]["c"] if before else None

            ip, sp = price_at(iex), price_at(sip)
            if ip and sp:
                p1540_gap.append(abs(ip - sp) / sp)
    log.event(
        "iex_vs_sip",
        days=days,
        symbols=len(sample),
        orh_shortfall_frac=summary(orh_gap),
        orh_iex_below_sip=sum(1 for g in orh_gap if g > 1e-9),
        orh_equal=sum(1 for g in orh_gap if abs(g) <= 1e-9),
        orh_iex_missing_first5=orh_missing,
        price_1540_abs_diff_frac=summary(p1540_gap),
        iex_volume_share=summary(vol_share),
        iex_minutes_with_trades_vs_sip=summary(iex_minutes),
        worst_orh=sorted(rows, key=lambda r: r["iex_orh"] / r["sip_orh"])[:8],
    )


def scenario_orh_liquidity(args: argparse.Namespace) -> None:
    """IEX opening-range coverage by liquidity bucket (eligible: >= $10, ADV20 >= $10M)."""
    _, log, http = spike("orh_liquidity")
    universe = load_universe()["common"]
    today = datetime.now(ET).date()
    days = sessions(http, today - timedelta(days=40), today - timedelta(days=1))
    daily: dict[str, list[dict[str, Any]]] = {}
    for chunk in chunks(universe, 500):
        daily.update(
            http.bars(
                chunk,
                "1Day",
                days[-20].isoformat(),
                days[-1].isoformat(),
                feed="sip",
                adjustment="raw",
            )
        )
    adv = {
        s: statistics.mean(r["c"] * r["v"] for r in rows)
        for s, rows in daily.items()
        if len(rows) >= 15 and rows[-1]["c"] >= 10
    }
    eligible = sorted((s for s in adv if adv[s] >= 10e6), key=lambda s: -adv[s])
    buckets = {
        "rank 1-100": eligible[:100],
        "rank 101-500": eligible[100:500],
        "rank 501-1000": eligible[500:1000],
        "rank 1001+": eligible[1000:],
    }
    log.event("eligible", count=len(eligible), sizes={k: len(v) for k, v in buckets.items()})
    first5 = {(9, m) for m in range(30, 35)}
    for name, members in buckets.items():
        pick = members[:: max(1, len(members) // 40)][:40]
        missing, total, short, late = 0, 0, [], []
        for day in days[-5:]:
            mins = {
                f: http.bars(pick, "1Min", et(day, 9, 30), et(day, 9, 50), feed=f)
                for f in ("iex", "sip")
            }
            for sym in pick:
                sip = [
                    m
                    for m in mins["sip"].get(sym, [])
                    if (bar_et(m).hour, bar_et(m).minute) in first5
                ]
                iex_all = mins["iex"].get(sym, [])
                iex = [m for m in iex_all if (bar_et(m).hour, bar_et(m).minute) in first5]
                if not sip:
                    continue
                total += 1
                if not iex:
                    missing += 1
                    late.append(bar_et(iex_all[0]).minute if iex_all else None)
                    continue
                sip_h, iex_h = max(m["h"] for m in sip), max(m["h"] for m in iex)
                short.append((sip_h - iex_h) / sip_h)
        log.event(
            "bucket",
            bucket=name,
            adv_musd=(round(adv[members[-1]] / 1e6, 1), round(adv[members[0]] / 1e6, 1)),
            symbol_days=total,
            no_iex_trade_0930_0934=missing,
            orh_shortfall=summary(short),
            first_iex_minute_when_missing=sorted(late, key=lambda x: (x is None, x or 0)),
        )


def scenario_iex_depth(args: argparse.Namespace) -> None:
    _, log, http = spike("iex_depth")
    found = {}
    for year in range(2015, 2027):
        for month in (1, 7):
            day = date(year, month, 15)
            got = http.bars(
                ["SPY", "AAPL"],
                "1Min",
                et(day, 9, 30),
                et(day + timedelta(days=4), 16, 0),
                feed="iex",
            ).get("SPY", [])
            found[day.isoformat()] = len(got)
    log.event("iex_minute_bars_by_probe_date", spy_bar_counts=found)
    first = [d for d, n in found.items() if n]
    if first:
        lo = date.fromisoformat(first[0]) - timedelta(days=200)
        hi = date.fromisoformat(first[0])
        while (hi - lo).days > 2:
            mid = lo + (hi - lo) / 2
            got = http.bars(["SPY"], "1Day", mid.isoformat(), hi.isoformat(), feed="iex")
            n_bars = len(got.get("SPY", []))
            if n_bars >= ((hi - mid).days * 5) // 7 - 3:
                hi = mid
            else:
                lo = mid
        daily = http.bars(
            ["SPY"], "1Day", lo.isoformat(), (hi + timedelta(days=10)).isoformat(), feed="iex"
        ).get("SPY", [])
        log.event("iex_first_daily_bars", around=hi, bars=[r["t"][:10] for r in daily[:5]])
        minute = http.bars(
            ["SPY"],
            "1Min",
            daily[0]["t"][:10] if daily else hi.isoformat(),
            (hi + timedelta(days=10)).isoformat(),
            feed="iex",
        ).get("SPY", [])
        log.event("iex_first_minute_bars", first=[r["t"] for r in minute[:3]])


# --- pre-market gaps ---------------------------------------------------------------------


def scenario_premarket(args: argparse.Namespace) -> None:
    _, log, http = spike("premarket")
    universe = load_universe()["common"]
    today = datetime.now(ET).date()
    days = sessions(http, today - timedelta(days=200), today - timedelta(days=1))
    start, end = days[0], days[-1]
    daily: dict[str, list[dict[str, Any]]] = {}
    for chunk in chunks(universe, 200):
        daily.update(
            http.bars(
                chunk, "1Day", start.isoformat(), end.isoformat(), feed="sip", adjustment="split"
            )
        )
    gaps, near = [], []
    for sym, rows in daily.items():
        for prev, cur in itertools.pairwise(rows):
            if prev["c"] < 10 or prev["c"] * prev["v"] < 10_000_000:
                continue
            g = cur["o"] / prev["c"] - 1
            event = {
                "symbol": sym,
                "day": cur["t"][:10],
                "prev_close": prev["c"],
                "open": cur["o"],
                "gap": g,
            }
            if g >= 0.10:
                gaps.append(event)
            elif 0.05 <= g < 0.10:
                near.append(event)
    log.event("gap_events", sessions=len(days), gaps_ge_10=len(gaps), gaps_5_to_10=len(near))
    step_g = max(1, len(gaps) // 80)
    step_n = max(1, len(near) // 80)
    sample = gaps[::step_g][:80] + near[::step_n][:80]
    results = []
    for ev in sample:
        day = date.fromisoformat(ev["day"])
        pre = http.bars([ev["symbol"]], "1Min", et(day, 4, 0), et(day, 8, 40), feed="iex").get(
            ev["symbol"], []
        )
        from8 = [m for m in pre if bar_et(m).hour >= 8]
        quote = http.get(
            f"{DATA}/v2/stocks/{ev['symbol']}/quotes",
            {"feed": "iex", "start": et(day, 8, 30), "end": et(day, 8, 40), "limit": 1000},
        )
        quotes = quote.json().get("quotes") or [] if quote.status_code == 200 else []
        good = [q for q in quotes if q["bp"] > 0 and q["ap"] > 0]
        est_trade = pre[-1]["c"] / ev["prev_close"] - 1 if pre else None
        est_mid = ((good[-1]["bp"] + good[-1]["ap"]) / 2) / ev["prev_close"] - 1 if good else None
        results.append(
            dict(
                ev,
                iex_premarket_bars=len(pre),
                iex_bars_from_0800=len(from8),
                iex_volume_to_0840=sum(m["v"] for m in pre),
                est_gap_last_trade=est_trade,
                est_gap_quote_mid=est_mid,
                spread_frac=(good[-1]["ap"] - good[-1]["bp"]) / good[-1]["ap"] if good else None,
            )
        )
    real = [r for r in results if r["gap"] >= 0.10]
    nearr = [r for r in results if r["gap"] < 0.10]
    log.event(
        "premarket_gap_estimates",
        sampled_gaps=len(real),
        with_iex_trade_by_0840=sum(1 for r in real if r["est_gap_last_trade"] is not None),
        with_iex_quote_0830_0840=sum(1 for r in real if r["est_gap_quote_mid"] is not None),
        detected_ge_10_by_trade=sum(1 for r in real if (r["est_gap_last_trade"] or 0) >= 0.10),
        detected_ge_10_by_mid=sum(1 for r in real if (r["est_gap_quote_mid"] or 0) >= 0.10),
        error_trade=summary(
            [
                r["est_gap_last_trade"] - r["gap"]
                for r in real
                if r["est_gap_last_trade"] is not None
            ]
        ),
        error_mid=summary(
            [r["est_gap_quote_mid"] - r["gap"] for r in real if r["est_gap_quote_mid"] is not None]
        ),
        spread=summary([r["spread_frac"] for r in real if r["spread_frac"] is not None]),
        sampled_near=len(nearr),
        near_false_positive_trade=sum(1 for r in nearr if (r["est_gap_last_trade"] or 0) >= 0.10),
        near_false_positive_mid=sum(1 for r in nearr if (r["est_gap_quote_mid"] or 0) >= 0.10),
        near_with_trade=sum(1 for r in nearr if r["est_gap_last_trade"] is not None),
    )
    (OUT / "premarket_rows.json").write_text(json.dumps(results, default=str), encoding="utf-8")


# --- survivorship ------------------------------------------------------------------------


def scenario_survivorship(args: argparse.Namespace) -> None:
    _, log, http = spike("survivorship")
    uni = load_universe()
    status: dict[str, str] = uni["status"]
    rows = []
    known = {} if args.skip_known else KNOWN_DELISTED
    for sym, (year, why) in known.items():
        got = http.bars(
            [sym], "1Day", "2015-12-01", f"{year + 1}-12-31", feed="sip", adjustment="split"
        ).get(sym, [])
        rows.append(
            {
                "symbol": sym,
                "year": year,
                "why": why,
                "in_assets": status.get(sym, "absent"),
                "bars": len(got),
                "first": got[0]["t"][:10] if got else None,
                "last": got[-1]["t"][:10] if got else None,
            }
        )
    covered = [r for r in rows if r["bars"]]
    full = [r for r in covered if r["first"] <= "2016-01-05" and r["last"] >= f"{r['year']}-01-01"]
    log.event(
        "known_delistings",
        sample=len(rows),
        with_any_bars=len(covered),
        full_history_to_delisting_year=len(full),
        by_year={
            y: f"{sum(r['year'] == y for r in covered)}/{sum(r['year'] == y for r in rows)}"
            for y in sorted({r["year"] for r in rows})
        },
        rows=rows,
    )

    # Alpaca's own inactive listed symbols: when did their bars stop? Junk symbols (CUSIP-like
    # codes) make the whole request fail, and delisted assets are renamed "<SYM>_DELISTED".
    raw_inactive = uni["inactive_listed"]
    renamed = sum(1 for s in raw_inactive if s.endswith("_DELISTED"))
    inactive = sorted({s.removesuffix("_DELISTED") for s in raw_inactive} - set(uni["common"]))
    inactive = [s for s in inactive if VALID_SYMBOL.fullmatch(s)]
    log.event(
        "inactive_cleaning",
        raw=len(raw_inactive),
        renamed_delisted=renamed,
        junk=len(raw_inactive)
        - renamed
        - sum(1 for s in raw_inactive if VALID_SYMBOL.fullmatch(s)),
        usable=len(inactive),
    )
    sample = inactive[:: max(1, len(inactive) // 400)][:400]
    last_bar: dict[str, str] = {}
    first_bar: dict[str, str] = {}
    for chunk in chunks(sample, 100):
        got = http.bars(
            chunk,
            "1Day",
            "2016-01-01",
            (datetime.now(ET).date() - timedelta(days=1)).isoformat(),
            feed="sip",
            adjustment="split",
        )
        for sym, bars in got.items():
            traded = [b for b in bars if b["v"] > 0]  # zero-volume bars repeat after delisting
            if traded:
                first_bar[sym], last_bar[sym] = traded[0]["t"][:10], traded[-1]["t"][:10]
    log.event(
        "inactive_listed_sample",
        inactive_listed_total=len(inactive),
        sampled=len(sample),
        with_bars=len(last_bar),
        last_bar_year=Counter(v[:4] for v in last_bar.values()),
        first_bar_year=Counter(v[:4] for v in first_bar.values()),
    )

    # Universe size by year: symbols with any bar in the first week of each year.
    names = sorted(set(uni["common"]) | set(inactive))
    per_year: dict[int, int] = {}
    for year in (2016, 2018, 2020, 2022, 2024, 2026):
        n = 0
        for chunk in chunks(names, 1000):
            got = http.bars(
                chunk, "1Day", f"{year}-01-04", f"{year}-01-08", feed="sip", adjustment="raw"
            )
            n += sum(1 for bars in got.values() if any(b["v"] > 0 for b in bars))
        per_year[year] = n
    log.event("symbols_with_bars_first_week_of_year", counts=per_year, candidates=len(names))


# --- real-time IEX (market hours) --------------------------------------------------------


def scenario_realtime(args: argparse.Namespace) -> None:
    _, log, http = spike("realtime")
    universe = load_universe()["common"]
    sample = [*universe[:: max(1, len(universe) // 50)][:50], "SPY", "AAPL", "NVDA"]
    now = datetime.now(UTC)
    for kind in ("quotes", "trades", "bars"):
        response = http.get(
            f"{DATA}/v2/stocks/{kind}/latest", {"symbols": ",".join(sample), "feed": "iex"}
        )
        body = response.json().get(kind, {}) if response.status_code == 200 else {}
        ages = [
            (now - datetime.fromisoformat(v["t"].replace("Z", "+00:00"))).total_seconds()
            for v in body.values()
        ]
        log.event(
            f"latest_{kind}", status=response.status_code, n=len(body), age_seconds=summary(ages)
        )
    snap = http.get(f"{DATA}/v2/stocks/snapshots", {"symbols": "SPY,AAPL", "feed": "iex"})
    log.event(
        "snapshot",
        status=snap.status_code,
        body=snap.json() if snap.status_code == 200 else snap.text[:300],
    )
    response = http.get(
        f"{DATA}/v2/stocks/bars",
        {
            "symbols": "SPY",
            "timeframe": "1Min",
            "start": (now - timedelta(minutes=30)).isoformat(),
            "feed": "sip",
        },
    )
    log.event("sip_recent_window", status=response.status_code, body=response.text[:300])
    delayed = http.get(
        f"{DATA}/v2/stocks/bars",
        {
            "symbols": "SPY",
            "timeframe": "1Min",
            "start": (now - timedelta(minutes=40)).isoformat(),
            "end": (now - timedelta(minutes=16)).isoformat(),
            "feed": "sip",
        },
    )
    log.event(
        "sip_delayed_window",
        status=delayed.status_code,
        last=(delayed.json().get("bars", {}).get("SPY") or [{}])[-1]
        if delayed.status_code == 200
        else delayed.text[:300],
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="spikes.alpaca_data")
    sub = parser.add_subparsers(dest="scenario", required=True)
    for name in (
        "assets",
        "daily",
        "adjustments",
        "intraday",
        "iex_depth",
        "orh_liquidity",
        "premarket",
        "realtime",
    ):
        sub.add_parser(name)
    sub.add_parser("survivorship").add_argument("--skip-known", action="store_true")
    args = parser.parse_args()
    globals()[f"scenario_{args.scenario}"](args)


if __name__ == "__main__":
    main()
