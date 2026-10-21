"""SEC EDGAR: the ticker → CIK map, submissions (SIC, filings incl. 8-K Item 2.02) and XBRL
company facts (spike 0.3e, docs/spikes/fundamentals.md).

- SEC asks for a User-Agent with a real contact email and at most 10 requests a second.
- `acceptanceDateTime` is UTC (the trailing `Z` is real; checked against news timing).
- Submissions list about the last 1,000 filings in `recent`; older ones are in extra
  `files` pages, which a 2016 backfill needs.
- Company facts are keyed on `filed`, the date the market could see them (README §5.1).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from time import sleep as time_sleep
from typing import Any

import httpx

from auratrade.data.http import RateLimiter, VendorError, VendorHttp

DATA_URL = "https://data.sec.gov"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
REQUESTS_PER_SECOND = 8
EARNINGS_ITEM = "2.02"  # Results of Operations and Financial Condition


@dataclass(frozen=True)
class CompanyTicker:
    cik: int
    ticker: str  # SEC style: class shares use a dash (BRK-B), Alpaca a dot (BRK.B)
    title: str


@dataclass(frozen=True)
class Filing:
    accession: str
    form: str
    filed: date
    accepted_at: datetime  # UTC
    report_date: date | None
    items: tuple[str, ...]
    primary_document: str

    @property
    def is_earnings_release(self) -> bool:
        """An 8-K carrying Item 2.02: the historical earnings date (README §5.1, #119)."""
        return self.form == "8-K" and EARNINGS_ITEM in self.items


@dataclass(frozen=True)
class Submissions:
    cik: int
    name: str
    sic: str | None
    sic_description: str | None
    tickers: tuple[str, ...]
    exchanges: tuple[str, ...]
    filings: tuple[Filing, ...]  # newest first

    def earnings_releases(self) -> list[Filing]:
        return [f for f in self.filings if f.is_earnings_release]


@dataclass(frozen=True)
class FactValue:
    start: date | None  # None for point-in-time facts (balance-sheet values)
    end: date
    value: float
    accession: str
    fiscal_year: int | None
    fiscal_period: str | None  # Q1, Q2, Q3 or FY
    form: str
    filed: date
    frame: str | None


@dataclass(frozen=True)
class CompanyFacts:
    cik: int
    name: str
    facts: Mapping[str, Mapping[str, Mapping[str, tuple[FactValue, ...]]]]

    def values(self, tag: str, unit: str, *, taxonomy: str = "us-gaap") -> tuple[FactValue, ...]:
        return self.facts.get(taxonomy, {}).get(tag, {}).get(unit, ())


def cik10(cik: int) -> str:
    return f"CIK{cik:010d}"


class EdgarClient:
    def __init__(
        self,
        user_agent: str,
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time_sleep,
    ) -> None:
        if "@" not in user_agent:
            raise ValueError("EDGAR_USER_AGENT must include a contact email (SEC fair access)")
        self._http = VendorHttp(
            "edgar",
            limiter=RateLimiter(per_second=REQUESTS_PER_SECOND, sleep=sleep),
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            client=client,
            sleep=sleep,
        )

    def company_tickers(self) -> list[CompanyTicker]:
        body = self._http.get_json(TICKERS_URL)
        if not isinstance(body, dict):
            raise VendorError("edgar", "company_tickers.json: unexpected response", retryable=False)
        return [CompanyTicker(int(r["cik_str"]), r["ticker"], r["title"]) for r in body.values()]

    def submissions(self, cik: int, *, since: date | None = None) -> Submissions | None:
        """A company's profile and filings, None if EDGAR has no such CIK. Older filing
        pages are loaded only while they reach back to `since` (all of them if None)."""
        body = self._http.get_json_or_none(f"{DATA_URL}/submissions/{cik10(cik)}.json")
        if body is None:
            return None
        filings = body.get("filings") or {}
        rows = _filings(filings.get("recent") or {})
        for page in filings.get("files") or []:
            if since is not None and date.fromisoformat(page["filingTo"]) < since:
                continue
            older = self._http.get_json(f"{DATA_URL}/submissions/{page['name']}")
            rows.extend(_filings(older))
        rows.sort(key=lambda f: f.accepted_at, reverse=True)
        return Submissions(
            cik,
            body.get("name") or "",
            body.get("sic") or None,
            body.get("sicDescription") or None,
            tuple(body.get("tickers") or ()),
            tuple(e for e in body.get("exchanges") or () if e),
            tuple(rows),
        )

    def company_facts(self, cik: int) -> CompanyFacts | None:
        """XBRL facts, None when the company files none (e.g. most foreign filers' 10-Qs)."""
        body = self._http.get_json_or_none(f"{DATA_URL}/api/xbrl/companyfacts/{cik10(cik)}.json")
        if body is None:
            return None
        facts = {
            taxonomy: {
                tag: {
                    unit: tuple(_fact(v) for v in values)
                    for unit, values in (spec.get("units") or {}).items()
                }
                for tag, spec in tags.items()
            }
            for taxonomy, tags in (body.get("facts") or {}).items()
        }
        # Some filers' facts come without "cik" (closed-end funds, a few small companies).
        return CompanyFacts(int(body.get("cik") or cik), body.get("entityName") or "", facts)


def _filings(columns: Mapping[str, Sequence[Any]]) -> list[Filing]:
    """EDGAR's column arrays (one list per field) → rows."""
    forms = columns.get("form") or []
    out = []
    for i in range(len(forms)):
        report = columns["reportDate"][i]
        items = columns["items"][i] if "items" in columns else ""
        out.append(
            Filing(
                columns["accessionNumber"][i],
                forms[i],
                date.fromisoformat(columns["filingDate"][i]),
                datetime.fromisoformat(columns["acceptanceDateTime"][i]).astimezone(UTC),
                date.fromisoformat(report) if report else None,
                tuple(item for item in items.split(",") if item),
                columns["primaryDocument"][i] if "primaryDocument" in columns else "",
            )
        )
    return out


def _fact(raw: Mapping[str, Any]) -> FactValue:
    start = raw.get("start")
    return FactValue(
        date.fromisoformat(start) if start else None,
        date.fromisoformat(raw["end"]),
        float(raw["val"]),
        raw["accn"],
        raw.get("fy"),
        raw.get("fp"),
        raw["form"],
        date.fromisoformat(raw["filed"]),
        raw.get("frame"),
    )
