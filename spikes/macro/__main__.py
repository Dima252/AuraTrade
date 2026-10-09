"""Spike 0.3d: when CBOE VIX/VIX3M and FRED macro values become available.

    uv run --group spikes python -m spikes.macro snapshot            # history, CBOE vs FRED
    uv run --group spikes python -m spikes.macro fred_lag            # FRED lag from vintages
    uv run --group spikes python -m spikes.macro poll --until 22:00  # watch today's values

Findings: docs/spikes/macro.md.
"""

from __future__ import annotations

import argparse
import csv
import io
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pandas_market_calendars as pmc
from dotenv import dotenv_values

from spikes._common import ROOT, Log

HERE = Path(__file__).resolve().parent
NY = ZoneInfo("America/New_York")
CBOE_CSV = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{sym}_History.csv"
CBOE_QUOTE = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_{sym}.json"
FRED = "https://api.stlouisfed.org/fred"
# VIX, VIX3M (FRED calls it VXV) and the ICE BofA high-yield option-adjusted spread.
FRED_SERIES = {"VIX": "VIXCLS", "VIX3M": "VXVCLS", "HY_OAS": "BAMLH0A0HYM2"}
CBOE_SYMBOLS = ("VIX", "VIX3M")


def fred_key() -> str:
    key = dotenv_values(ROOT / ".env").get("FRED_API_KEY")
    if not key:
        raise SystemExit("FRED_API_KEY missing in .env")
    return key


def parse_cboe_csv(text: str) -> dict[date, float]:
    closes = {}
    for row in csv.DictReader(io.StringIO(text)):
        month, day, year = map(int, row["DATE"].split("/"))
        closes[date(year, month, day)] = float(row["CLOSE"])
    return closes


def cboe_csv(http: httpx.Client, sym: str) -> tuple[httpx.Response, dict[date, float]]:
    response = http.get(CBOE_CSV.format(sym=sym))
    return response, parse_cboe_csv(response.text) if response.status_code == 200 else {}


def cache_headers(response: httpx.Response) -> dict[str, str | None]:
    return {k: response.headers.get(k) for k in ("last-modified", "age", "cache-control", "etag")}


class FredError(Exception):
    pass


def fred_get(http: httpx.Client, key: str, path: str, **params: Any) -> dict[str, Any]:
    # FRED only accepts the key as a query parameter, and httpx puts the full URL in its
    # exception messages, so never let an httpx error escape: report the status alone.
    for attempt in range(3):
        try:
            response = http.get(
                f"{FRED}/{path}", params={**params, "api_key": key, "file_type": "json"}
            )
        except httpx.HTTPError as exc:
            raise FredError(f"{path}: {type(exc).__name__}") from None
        if response.status_code == 200:
            body: dict[str, Any] = response.json()
            return body
        if response.status_code < 500 or attempt == 2:
            raise FredError(f"{path}: HTTP {response.status_code}")
        time.sleep(2 * (attempt + 1))
    raise AssertionError("unreachable")


def fred_observations(
    http: httpx.Client, key: str, series: str, start: str = "2016-01-01", **params: Any
) -> dict[date, float]:
    body = fred_get(
        http, key, "series/observations", series_id=series, observation_start=start, **params
    )
    return {
        date.fromisoformat(o["date"]): float(o["value"])
        for o in body.get("observations", [])
        if o["value"] not in (".", "")
    }


def sessions(start: date, end: date) -> list[date]:
    days = pmc.get_calendar("NYSE").valid_days(start_date=start, end_date=end)
    return [d.date() for d in days]


def scenario_snapshot(args: argparse.Namespace) -> None:
    key = fred_key()
    log = Log(HERE, "snapshot", [key])
    today = datetime.now(NY).date()
    expected = sessions(date(2016, 1, 4), today - timedelta(days=1))
    with httpx.Client(timeout=60, follow_redirects=True) as http:
        cboe: dict[str, dict[date, float]] = {}
        for sym in CBOE_SYMBOLS:
            response, closes = cboe_csv(http, sym)
            cboe[sym] = closes
            since = {d for d in closes if d >= expected[0]}
            missing = [d for d in expected if d not in closes]
            extra = sorted(since - set(expected))
            log.event(
                "cboe_csv",
                symbol=sym,
                status=response.status_code,
                headers=cache_headers(response),
                first=min(closes),
                last=max(closes),
                rows_since_2016=len(since),
                expected_sessions=len(expected),
                missing_sessions=len(missing),
                missing_sample=missing[:15],
                non_session_rows=extra[:15],
            )
            quote = http.get(CBOE_QUOTE.format(sym=sym))
            data = quote.json().get("data", {}) if quote.status_code == 200 else {}
            log.event(
                "cboe_quote",
                symbol=sym,
                status=quote.status_code,
                headers=cache_headers(quote),
                timestamp=quote.json().get("timestamp") if quote.status_code == 200 else None,
                fields={
                    k: data.get(k)
                    for k in ("current_price", "close", "prev_day_close", "last_trade_time")
                },
            )

        for name, series in FRED_SERIES.items():
            meta = fred_get(http, key, "series", series_id=series)["seriess"][0]
            obs = fred_observations(http, key, series)
            log.event(
                "fred_series",
                name=name,
                series=series,
                last_updated=meta.get("last_updated"),
                observation_start=meta.get("observation_start"),
                observation_end=meta.get("observation_end"),
                first_since_2016=min(obs) if obs else None,
                last=max(obs) if obs else None,
                n_since_2016=len(obs),
                notes=(meta.get("notes") or "")[:300],
            )
            if name in cboe:
                common = sorted(set(obs) & set(cboe[name]))
                diffs = [
                    (d, cboe[name][d], obs[d])
                    for d in common
                    if abs(cboe[name][d] - obs[d]) > 0.005
                ]
                only_cboe = [d for d in expected if d in cboe[name] and d not in obs]
                log.event(
                    "cboe_vs_fred",
                    name=name,
                    common_days=len(common),
                    differing=len(diffs),
                    max_abs_diff=max((abs(a - b) for _, a, b in diffs), default=0.0),
                    diff_sample=diffs[:10],
                    sessions_missing_in_fred=len(only_cboe),
                    missing_in_fred_sample=only_cboe[-10:],
                )


def scenario_fred_lag(args: argparse.Namespace) -> None:
    """For each recent FRED vintage, the newest observation it contained -> publication lag."""
    key = fred_key()
    log = Log(HERE, "fred_lag", [key])
    with httpx.Client(timeout=60) as http:
        for name, series in FRED_SERIES.items():
            vintages = fred_get(
                http,
                key,
                "series/vintagedates",
                series_id=series,
                sort_order="desc",
                limit=args.vintages,
            )
            dates = sorted(vintages.get("vintage_dates", []))
            lags = []
            for v in dates:
                try:
                    obs = fred_observations(
                        http,
                        key,
                        series,
                        start=(date.fromisoformat(v) - timedelta(days=20)).isoformat(),
                        realtime_start=v,
                        realtime_end=v,
                    )
                except FredError as exc:
                    log.event("error", series=series, vintage=v, error=str(exc))
                    continue
                if not obs:
                    continue
                newest = max(obs)
                lag_sessions = len(sessions(newest, date.fromisoformat(v))) - 1
                lags.append({"vintage": v, "newest_obs": newest, "lag_sessions": lag_sessions})
                time.sleep(0.3)  # FRED allows 120 requests a minute
            counts: dict[int, int] = {}
            for row in lags:
                counts[row["lag_sessions"]] = counts.get(row["lag_sessions"], 0) + 1
            log.event(
                "fred_lag",
                name=name,
                series=series,
                vintages=len(lags),
                lag_histogram=counts,
                sample=lags[-10:],
            )


def scenario_poll(args: argparse.Namespace) -> None:
    """Watch for today's VIX/VIX3M in each source; log each change and when today's appears."""
    key = fred_key()
    log = Log(HERE, "poll", [key])
    h, m = map(int, args.until.split(":"))
    today = datetime.now(NY).date()
    stop_at = datetime.now(NY).replace(hour=h, minute=m, second=0, microsecond=0)
    seen: dict[str, Any] = {}
    first_today: dict[str, str] = {}

    def note(source: str, state: Any, has_today: bool) -> None:
        now = datetime.now(NY).isoformat(timespec="seconds")
        if seen.get(source) != state:
            seen[source] = state
            log.event("change", source=source, state=state, has_today=has_today)
        if has_today and source not in first_today:
            first_today[source] = now
            log.event("first_today", source=source, at_et=now, state=state)

    with httpx.Client(timeout=60, follow_redirects=True) as http:
        while datetime.now(NY) < stop_at:
            for sym in CBOE_SYMBOLS:
                try:
                    response, closes = cboe_csv(http, sym)
                    last = max(closes) if closes else None
                    note(
                        f"csv_{sym}",
                        {
                            "last_row": last,
                            "close": closes.get(last) if last else None,
                            "last_modified": response.headers.get("last-modified"),
                        },
                        last == today,
                    )
                    quote = http.get(CBOE_QUOTE.format(sym=sym)).json()
                    data = quote.get("data", {})
                    trade_time = data.get("last_trade_time") or ""
                    note(
                        f"quote_{sym}",
                        {"current_price": data.get("current_price"), "last_trade_time": trade_time},
                        trade_time >= f"{today.isoformat()}T16:15",
                    )
                except (httpx.HTTPError, ValueError) as exc:
                    log.event("error", source=sym, error=type(exc).__name__)
            for name in ("VIX", "VIX3M"):
                try:
                    obs = fred_observations(
                        http, key, FRED_SERIES[name], start=(today - timedelta(days=10)).isoformat()
                    )
                    last = max(obs) if obs else None
                    note(
                        f"fred_{name}",
                        {"last_obs": last, "value": obs.get(last) if last else None},
                        last == today,
                    )
                except FredError as exc:
                    log.event("error", source=f"fred_{name}", error=str(exc))
            time.sleep(args.every)
        log.event("summary", first_today=first_today, final=seen)


def main() -> None:
    parser = argparse.ArgumentParser(prog="spikes.macro")
    sub = parser.add_subparsers(dest="scenario", required=True)
    sub.add_parser("snapshot")
    sub.add_parser("fred_lag").add_argument("--vintages", type=int, default=60)
    p = sub.add_parser("poll")
    p.add_argument("--until", default="22:00", help="ET time to stop")
    p.add_argument("--every", type=int, default=120, help="seconds between polls")
    args = parser.parse_args()
    {"snapshot": scenario_snapshot, "fred_lag": scenario_fred_lag, "poll": scenario_poll}[
        args.scenario
    ](args)


if __name__ == "__main__":
    main()
