"""Spike 0.3e: fundamentals, earnings dates and industries (SEC EDGAR + Finnhub free tier).

    uv run --group spikes python -m spikes.fundamentals universe   # filtered universe + CIKs
    uv run --group spikes python -m spikes.fundamentals edgar      # SIC coverage, filer types
    uv run --group spikes python -m spikes.fundamentals calendar   # Finnhub upcoming dates
    uv run --group spikes python -m spikes.fundamentals history    # 8-K 2.02 vs news, 2016-25
    uv run --group spikes python -m spikes.fundamentals facts      # EPS/revenue extraction

Findings: docs/spikes/fundamentals.md. Raw output in spikes/fundamentals/output/ (git-ignored).
"""

from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import time
from collections import Counter, deque
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from dotenv import dotenv_values

from spikes._common import ROOT, Env, Log
from spikes.alpaca_data.__main__ import DATA, Http, chunks, load_universe, sessions

HERE = Path(__file__).resolve().parent
OUT = HERE / "output"
ET = ZoneInfo("America/New_York")
SEC = "https://data.sec.gov"
FINNHUB = "https://finnhub.io/api/v1"
FILTERED = OUT / "universe_filtered.json"
EDGAR_META = OUT / "edgar_meta.json"
SAMPLE = OUT / "sample.json"
EARNINGS_WORDS = re.compile(
    r"\b(Q[1-4]|quarter|quarterly|EPS|earnings|results|revenue|sales)\b", re.I
)
REVENUE_TAGS = (
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
    "RevenuesNetOfInterestExpense",
)
EPS_TAGS = (
    "EarningsPerShareDiluted",
    "EarningsPerShareBasicAndDiluted",
    "IncomeLossFromContinuingOperationsPerDilutedShare",
    "EarningsPerShareBasic",
)


class Limiter:
    """At most `per_second` calls per rolling second (SEC allows 10; Finnhub 30/s, 60/min)."""

    def __init__(self, per_second: float, per_minute: int | None = None) -> None:
        self.gap = 1.0 / per_second
        self.per_minute = per_minute
        self.last = 0.0
        self.minute: deque[float] = deque()

    def wait(self) -> None:
        now = time.monotonic()
        if self.per_minute:
            while self.minute and now - self.minute[0] > 60:
                self.minute.popleft()
            if len(self.minute) >= self.per_minute:
                time.sleep(60.5 - (now - self.minute[0]))
                now = time.monotonic()
            self.minute.append(now)
        delay = self.last + self.gap - now
        if delay > 0:
            time.sleep(delay)
        self.last = time.monotonic()


class Sec:
    def __init__(self, log: Log) -> None:
        agent = dotenv_values(ROOT / ".env").get("EDGAR_USER_AGENT")
        if not agent or "@" not in agent:
            raise SystemExit("EDGAR_USER_AGENT with a real email is required in .env")
        self.client = httpx.Client(
            headers={"User-Agent": agent, "Accept-Encoding": "gzip, deflate"}, timeout=60
        )
        self.limit = Limiter(6)
        self.log = log

    def get(self, url: str) -> dict[str, Any] | None:
        for attempt in range(4):
            self.limit.wait()
            try:
                response = self.client.get(url)
            except httpx.HTTPError as exc:
                self.log.event("sec_error", url=url, error=type(exc).__name__)
                time.sleep(2 * (attempt + 1))
                continue
            if response.status_code == 200:
                body: dict[str, Any] = response.json()
                return body
            if response.status_code == 404:
                return None
            self.log.event("sec_http", url=url, status=response.status_code)
            time.sleep(5 * (attempt + 1))
        return None


class Finnhub:
    def __init__(self, log: Log) -> None:
        key = dotenv_values(ROOT / ".env").get("FINNHUB_API_KEY")
        if not key:
            raise SystemExit("FINNHUB_API_KEY missing in .env")
        # The key goes in a header, so it never appears in a URL or an httpx error message.
        self.client = httpx.Client(headers={"X-Finnhub-Token": key}, timeout=60)
        self.limit = Limiter(2, per_minute=55)
        self.log = log

    def get(self, path: str, **params: Any) -> Any:
        for attempt in range(4):
            self.limit.wait()
            response = self.client.get(f"{FINNHUB}/{path}", params=params)
            if response.status_code == 200:
                return response.json()
            self.log.event("finnhub_http", path=path, status=response.status_code)
            time.sleep(10 * (attempt + 1))
        return None


def cik10(cik: int | str) -> str:
    return f"{int(cik):010d}"


def summary(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "p10": ordered[len(ordered) // 10],
        "median": statistics.median(ordered),
        "p90": ordered[min(len(ordered) - 1, len(ordered) * 9 // 10)],
    }


# --- universe ----------------------------------------------------------------------------


def scenario_universe(args: argparse.Namespace) -> None:
    """Spike-grade universe: last close >= $10 and 50-session average dollar volume >= $10M."""
    env = Env()
    log = Log(HERE, "universe", env.secrets())
    http = Http(env, log)
    common: list[str] = load_universe()["common"]
    today = datetime.now(ET).date()
    days = sessions(http, today - timedelta(days=90), today - timedelta(days=1))[-50:]
    stats: dict[str, tuple[float, float, int]] = {}
    for batch in chunks(common, 200):
        bars = http.bars(
            batch, "1Day", days[0].isoformat(), days[-1].isoformat(), feed="sip", adjustment="raw"
        )
        for sym, rows in bars.items():
            if rows:
                dollar = [r["c"] * r["v"] for r in rows]
                stats[sym] = (rows[-1]["c"], sum(dollar) / len(days), len(rows))
    kept = sorted(s for s, (close, dv, _) in stats.items() if close >= 10 and dv >= 10e6)

    sec = Sec(log)
    tickers = sec.client.get("https://www.sec.gov/files/company_tickers.json").json()
    by_ticker = {row["ticker"].upper(): int(row["cik_str"]) for row in tickers.values()}
    ciks = {s: by_ticker.get(s.replace(".", "-")) for s in kept}
    OUT.mkdir(parents=True, exist_ok=True)
    FILTERED.write_text(
        json.dumps({s: {"cik": ciks[s], "dollar_volume": stats[s][1]} for s in kept}),
        encoding="utf-8",
    )
    log.event(
        "universe",
        common_symbols=len(common),
        with_bars=len(stats),
        kept=len(kept),
        sessions=[days[0], days[-1]],
        with_cik=sum(1 for c in ciks.values() if c),
        no_cik_sample=[s for s, c in ciks.items() if not c][:40],
    )


def load_filtered() -> dict[str, dict[str, Any]]:
    data: dict[str, dict[str, Any]] = json.loads(FILTERED.read_text(encoding="utf-8"))
    return data


# --- EDGAR metadata and industries -------------------------------------------------------


def scenario_edgar(args: argparse.Namespace) -> None:
    log = Log(HERE, "edgar", [])
    sec = Sec(log)
    universe = load_filtered()
    meta: dict[str, dict[str, Any]] = {}
    for sym, row in universe.items():
        if not row["cik"]:
            continue
        body = sec.get(f"{SEC}/submissions/CIK{cik10(row['cik'])}.json")
        if body is None:
            continue
        recent = body["filings"]["recent"]
        forms = Counter(recent["form"])
        last_202 = max(
            (
                d
                for d, f, items in zip(
                    recent["filingDate"], recent["form"], recent["items"], strict=False
                )
                if f == "8-K" and "2.02" in (items or "")
            ),
            default=None,
        )
        meta[sym] = {
            "cik": row["cik"],
            "name": body.get("name"),
            "sic": body.get("sic") or None,
            "sic_description": body.get("sicDescription"),
            "entity_type": body.get("entityType"),
            "fiscal_year_end": body.get("fiscalYearEnd"),
            "files_10q": forms.get("10-Q", 0),
            "files_20f_40f": forms.get("20-F", 0) + forms.get("40-F", 0),
            "files_6k": forms.get("6-K", 0),
            "last_8k_202": last_202,
            "dollar_volume": row["dollar_volume"],
        }
    EDGAR_META.write_text(json.dumps(meta), encoding="utf-8")

    with_sic = {s: m for s, m in meta.items() if m["sic"]}
    foreign = [s for s, m in meta.items() if m["files_20f_40f"] and not m["files_10q"]]
    log.event(
        "coverage",
        universe=len(universe),
        with_cik=sum(1 for r in universe.values() if r["cik"]),
        with_submissions=len(meta),
        with_sic=len(with_sic),
        sic_6770_blank_check=[s for s, m in with_sic.items() if m["sic"] == "6770"],
        foreign_filers=len(foreign),
        foreign_sample=foreign[:20],
        domestic_no_recent_8k_202=[
            s for s, m in meta.items() if m["files_10q"] and not m["last_8k_202"]
        ][:40],
    )
    sics = Counter(m["sic"] for m in with_sic.values())
    log.event(
        "sic_distribution",
        distinct_sic4=len(sics),
        largest=[
            (s, n, next(m["sic_description"] for m in with_sic.values() if m["sic"] == s))
            for s, n in sics.most_common(25)
        ],
        singletons=sum(1 for n in sics.values() if n == 1),
    )
    # Roll small SIC codes up (4 -> 3 -> 2 digits) until each group has min_size members.
    for min_size in (5, 8, 10, 12, 15, 20):
        groups = rollup(sics, min_size)
        sizes = Counter(groups.values())
        log.event(
            "rollup",
            min_size=min_size,
            groups=len(set(groups.values())),
            largest=[
                (g, sizes[g])
                for g, _ in Counter(
                    {
                        g: sum(n for s, n in sics.items() if groups[s] == g)
                        for g in set(groups.values())
                    }
                ).most_common(8)
            ],
        )


def rollup(sics: Counter[str], min_size: int) -> dict[str, str]:
    """Map each 4-digit SIC to itself, its 3-digit prefix or its 2-digit prefix."""
    group = {s: s for s in sics}
    for width in (3, 2):
        totals: Counter[str] = Counter()
        for s, n in sics.items():
            totals[group[s]] += n
        for s in sics:
            if totals[group[s]] < min_size:
                group[s] = s[:width]
    return group


# --- Finnhub calendar and surprises ------------------------------------------------------


def scenario_calendar(args: argparse.Namespace) -> None:
    log = Log(HERE, "calendar", [])
    fh = Finnhub(log)
    universe = load_filtered()
    meta: dict[str, dict[str, Any]] = (
        json.loads(EDGAR_META.read_text(encoding="utf-8")) if EDGAR_META.exists() else {}
    )
    today = datetime.now(ET).date()
    events: list[dict[str, Any]] = []
    capped = []
    day = today
    while day <= today + timedelta(days=args.days):
        if day.weekday() < 5:
            body = fh.get("calendar/earnings", **{"from": day.isoformat(), "to": day.isoformat()})
            rows = (body or {}).get("earningsCalendar", [])
            if len(rows) >= 1500:
                capped.append(day)
            events.extend(rows)
        day += timedelta(days=1)
    snapshot = OUT / f"finnhub_calendar_{today.isoformat()}.json"
    snapshot.write_text(json.dumps(events), encoding="utf-8")

    first: dict[str, dict[str, Any]] = {}
    for e in sorted(events, key=lambda e: e["date"]):
        first.setdefault(e["symbol"], e)
    horizon = sessions(Http(Env(), log), today, today + timedelta(days=args.days))
    within_63 = horizon[min(62, len(horizon) - 1)].isoformat()
    covered = {s for s in universe if s in first and first[s]["date"] <= within_63}
    domestic = {s for s, m in meta.items() if m["files_10q"]}
    foreign = {s for s, m in meta.items() if m["files_20f_40f"] and not m["files_10q"]}
    log.event(
        "calendar",
        days_queried=args.days,
        events=len(events),
        capped_days=capped,
        universe=len(universe),
        next_date_within_63_sessions=len(covered),
        share=round(len(covered) / len(universe), 3),
        domestic_share=round(len(covered & domestic) / max(1, len(domestic)), 3),
        foreign_share=round(len(covered & foreign) / max(1, len(foreign)), 3),
        hour=Counter(first[s]["hour"] or "blank" for s in covered),
        has_eps_estimate=sum(1 for s in covered if first[s]["epsEstimate"] is not None),
        uncovered_domestic_sample=sorted(domestic - covered)[:40],
    )

    # Past dates: the free calendar returns none (checked at several dates back to 2019).
    past = {
        d: len(
            (fh.get("calendar/earnings", **{"from": d, "to": d}) or {}).get("earningsCalendar", [])
        )
        for d in ("2026-07-30", "2025-07-31", "2022-07-28", "2019-07-30")
    }
    log.event("calendar_history", events_on_past_days=past)

    rng = random.Random(7)  # noqa: S311 - a reproducible sample, not security
    sample = rng.sample(sorted(domestic & set(universe)), 50)
    quarters, fields, surprise_pct = Counter(), Counter(), []
    for sym in sample:
        rows = fh.get("stock/earnings", symbol=sym) or []
        quarters[len(rows)] += 1
        for r in rows:
            fields.update(k for k, v in r.items() if v is not None)
            if r.get("surprisePercent") is not None:
                surprise_pct.append(abs(r["surprisePercent"]))
    log.event(
        "surprises",
        symbols=len(sample),
        quarters_returned=quarters,
        fields_present=fields,
        abs_surprise_pct=summary(surprise_pct),
    )


# --- earnings history: 8-K Item 2.02 vs news ---------------------------------------------


def sec_time(raw: str) -> datetime:
    """EDGAR's acceptanceDateTime is UTC: news timestamps agree with it in 1,416 of 1,611 events."""
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def timing(moment: datetime) -> str:
    local = moment.astimezone(ET)
    hm = local.hour * 60 + local.minute
    return "bmo" if hm < 9 * 60 + 30 else "amc" if hm >= 16 * 60 else "dmh"


def reaction_session(moment: datetime, session_days: list[date]) -> date | None:
    """The first session whose price can react: same day if before the close, else the next."""
    local = moment.astimezone(ET)
    day = local.date()
    if local.hour >= 16 or day not in session_days:
        return next((d for d in session_days if d > day), None)
    return day


def filings_202(sec: Sec, cik: int, start: date, end: date) -> list[dict[str, str]]:
    body = sec.get(f"{SEC}/submissions/CIK{cik10(cik)}.json")
    if body is None:
        return []
    blocks = [body["filings"]["recent"]]
    for extra in body["filings"].get("files", []):
        if extra.get("filingTo", "9999") >= start.isoformat():
            more = sec.get(f"{SEC}/submissions/{extra['name']}")
            if more:
                blocks.append(more)
    out = []
    for b in blocks:
        for form, filed, accepted, items in zip(
            b["form"], b["filingDate"], b["acceptanceDateTime"], b["items"], strict=False
        ):
            if (
                form in ("8-K", "8-K/A")
                and "2.02" in (items or "")
                and start.isoformat() <= filed <= end.isoformat()
            ):
                out.append({"form": form, "filed": filed, "accepted": accepted})
    return sorted(out, key=lambda r: r["accepted"])


def scenario_history(args: argparse.Namespace) -> None:
    env = Env()
    log = Log(HERE, "history", env.secrets())
    http = Http(env, log)
    sec = Sec(log)
    meta: dict[str, dict[str, Any]] = json.loads(EDGAR_META.read_text(encoding="utf-8"))
    domestic = sorted(s for s, m in meta.items() if m["files_10q"])
    by_dv = sorted(domestic, key=lambda s: -meta[s]["dollar_volume"])
    rng = random.Random(11)  # noqa: S311 - a reproducible sample, not security
    sample = rng.sample(by_dv[:400], 25) + rng.sample(by_dv[400:], 25)
    SAMPLE.write_text(json.dumps(sample), encoding="utf-8")
    start, end = date(2016, 1, 1), date(2025, 12, 31)
    session_days = sessions(http, start - timedelta(days=10), end + timedelta(days=10))

    per_year: Counter[int] = Counter()
    agree, disagree, no_news, offsets = 0, [], 0, []
    tz_votes: Counter[str] = Counter()
    timing_8k: Counter[str] = Counter()
    years_short: list[tuple[str, int, int]] = []
    for sym in sample:
        events = [e for e in filings_202(sec, meta[sym]["cik"], start, end) if e["form"] == "8-K"]
        years = Counter(int(e["filed"][:4]) for e in events)
        per_year.update(years)
        years_short += [(sym, y, years.get(y, 0)) for y in range(2016, 2026) if years.get(y, 0) < 4]
        for e in events:
            accepted = sec_time(e["accepted"])
            as_if_et = datetime.fromisoformat(e["accepted"].replace("Z", "")).replace(tzinfo=ET)
            window = {
                "symbols": sym,
                "start": (accepted - timedelta(days=3)).astimezone(UTC).isoformat(),
                "end": (accepted + timedelta(days=1)).astimezone(UTC).isoformat(),
                "limit": 50,
                "sort": "asc",
            }
            response = http.get(f"{DATA}/v1beta1/news", window)
            news = response.json().get("news", []) if response.status_code == 200 else []
            hits = [n for n in news if EARNINGS_WORDS.search(n["headline"] or "")]
            if not hits:
                no_news += 1
                continue
            # Closest earnings headline to the filing: the release itself, not a preview piece.
            first = min(
                (datetime.fromisoformat(n["created_at"].replace("Z", "+00:00")) for n in hits),
                key=lambda t: abs((t - accepted).total_seconds()),
            )
            tz_votes["et"] += abs((first - as_if_et).total_seconds()) < abs(
                (first - accepted).total_seconds()
            )
            tz_votes["utc"] += abs((first - accepted).total_seconds()) < abs(
                (first - as_if_et).total_seconds()
            )
            timing_8k[timing(accepted)] += 1
            offsets.append((accepted - first).total_seconds() / 60)
            if reaction_session(accepted, session_days) == reaction_session(first, session_days):
                agree += 1
            else:
                disagree.append((sym, e["accepted"], first.astimezone(ET).isoformat()))
    log.event(
        "history",
        sample=len(sample),
        events=sum(per_year.values()),
        per_year=dict(sorted(per_year.items())),
        years_with_fewer_than_4=len(years_short),
        short_sample=years_short[:30],
        acceptance_time_zone_votes=tz_votes,
        timing_8k=timing_8k,
        compared=agree + len(disagree),
        same_reaction_session=agree,
        differs=len(disagree),
        differ_sample=disagree[:25],
        no_earnings_news=no_news,
        minutes_8k_after_first_news=summary(offsets),
    )


# --- EPS and revenue from company facts --------------------------------------------------


def quarter_values(
    facts: dict[str, Any], tags: tuple[str, ...], unit: str
) -> tuple[dict[date, tuple[float, date]], Counter[str]]:
    """Period end -> (3-month value, first filed date); Q4 = 10-K annual minus three quarters."""
    used: Counter[str] = Counter()
    quarters: dict[date, tuple[float, date]] = {}
    annual: dict[date, tuple[float, date, date]] = {}
    gaap = facts.get("facts", {}).get("us-gaap", {})
    for tag in tags:
        for row in gaap.get(tag, {}).get("units", {}).get(unit, []):
            if "start" not in row or row.get("form") not in ("10-Q", "10-K", "10-Q/A", "10-K/A"):
                continue
            s, e = date.fromisoformat(row["start"]), date.fromisoformat(row["end"])
            filed = date.fromisoformat(row["filed"])
            days = (e - s).days
            if 80 <= days <= 100 and (e not in quarters or filed < quarters[e][1]):
                if e not in quarters:
                    used[tag] += 1
                quarters[e] = (float(row["val"]), filed)
            elif 350 <= days <= 380 and (e not in annual or filed < annual[e][2]):
                annual[e] = (float(row["val"]), filed, s)
    for end, (value, filed, s) in annual.items():
        if end in quarters:
            continue
        inside = [q for q in quarters if s < q < end and (end - q).days > 20]
        if len(inside) == 3:
            quarters[end] = (value - sum(quarters[q][0] for q in inside), filed)
            used["derived_q4"] += 1
    return quarters, used


def scenario_facts(args: argparse.Namespace) -> None:
    log = Log(HERE, "facts", [])
    sec = Sec(log)
    meta: dict[str, dict[str, Any]] = json.loads(EDGAR_META.read_text(encoding="utf-8"))
    sample: list[str] = json.loads(SAMPLE.read_text(encoding="utf-8"))
    lo, hi = date(2016, 1, 1), date(2025, 12, 31)
    coverage, lags, tags_rev, tags_eps, weak = [], [], Counter(), Counter(), []
    for sym in sample:
        facts = sec.get(f"{SEC}/api/xbrl/companyfacts/CIK{cik10(meta[sym]['cik'])}.json")
        if facts is None:
            weak.append((sym, "no companyfacts"))
            continue
        rev, used_r = quarter_values(facts, REVENUE_TAGS, "USD")
        eps, used_e = quarter_values(facts, EPS_TAGS, "USD/shares")
        tags_rev.update(used_r)
        tags_eps.update(used_e)
        rev_in = sorted(d for d in rev if lo <= d <= hi)
        eps_in = {d for d in eps if lo <= d <= hi}
        if not rev_in:
            weak.append((sym, "no quarterly revenue"))
            continue
        expected = round((rev_in[-1] - rev_in[0]).days / 91.3) + 1
        both = sum(1 for d in rev_in if d in eps_in)
        coverage.append(both / expected)
        if both / expected < 0.9:
            weak.append((sym, f"{both}/{expected} quarters with revenue and EPS"))
        lags.extend((rev[d][1] - d).days for d in rev_in)
    log.event(
        "facts",
        sample=len(sample),
        quarter_coverage=summary(coverage),
        names_at_least_90pct=sum(1 for c in coverage if c >= 0.9),
        filed_minus_period_end_days=summary([float(x) for x in lags]),
        revenue_tags=tags_rev,
        eps_tags=tags_eps,
        weak=weak,
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="spikes.fundamentals")
    sub = parser.add_subparsers(dest="scenario", required=True)
    for name in ("universe", "edgar", "history", "facts"):
        sub.add_parser(name)
    sub.add_parser("calendar").add_argument("--days", type=int, default=100)
    args = parser.parse_args()
    {
        "universe": scenario_universe,
        "edgar": scenario_edgar,
        "calendar": scenario_calendar,
        "history": scenario_history,
        "facts": scenario_facts,
    }[args.scenario](args)


if __name__ == "__main__":
    main()
