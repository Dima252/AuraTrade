from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import pytest
from sqlalchemy import Engine, insert, select

from auratrade.calendar.sessions import MarketCalendar
from auratrade.data.edgar import CompanyFacts, FactValue
from auratrade.db.models import DailyBar, EarningsEvent, Fundamental
from auratrade.ingest.fundamentals import Quarter, ingest_fundamentals, quarters, write_quarters
from tests.ingest.fakes import RISK, add_ticker, sync_calendar

THU = date(2026, 10, 15)


def fact(
    start: date | None,
    end: date,
    value: float,
    filed: date,
    *,
    form: str = "10-Q",
    fy: int = 2025,
    fp: str = "Q1",
    accn: str = "a",
) -> FactValue:
    return FactValue(start, end, value, accn, fy, fp, form, filed, None)


def facts(
    revenue: list[FactValue], eps: list[FactValue], *, rev_tag: str = "Revenues"
) -> CompanyFacts:
    return CompanyFacts(
        1,
        "Co",
        {
            "us-gaap": {
                rev_tag: {"USD": tuple(revenue)},
                "EarningsPerShareDiluted": {"USD/shares": tuple(eps)},
            }
        },
    )


# Fiscal year = calendar year; quarters end Mar 31, Jun 30, Sep 30, Dec 31.
Q_ENDS = [date(2024, 3, 31), date(2024, 6, 30), date(2024, 9, 30), date(2024, 12, 31)]
Q_ENDS += [date(2025, 3, 31), date(2025, 6, 30), date(2025, 9, 30)]


def q_start(end: date) -> date:
    return {
        3: date(end.year, 1, 1),
        6: date(end.year, 4, 1),
        9: date(end.year, 7, 1),
        12: date(end.year, 10, 1),
    }[end.month]


def quarter_facts(values: dict[date, float], *, eps: bool = False) -> list[FactValue]:
    out = []
    for end, v in values.items():
        if end.month == 12:
            continue  # Q4 is never tagged
        fp = {3: "Q1", 6: "Q2", 9: "Q3"}[end.month]
        filed = (
            date(end.year, end.month % 12 + 1, 5) if end.month != 12 else date(end.year + 1, 1, 5)
        )
        out.append(fact(q_start(end), end, v, filed, fy=end.year, fp=fp))
    return out


class TestQuarters:
    def test_q4_derived_growth_and_acceleration(self) -> None:
        revenue = {e: 100.0 + 10 * i for i, e in enumerate(Q_ENDS)}  # 100 .. 160
        eps = {e: 1.0 + 0.1 * i for i, e in enumerate(Q_ENDS)}
        rev = quarter_facts(revenue)
        e = quarter_facts(eps)
        # FY2024 10-K: annual totals, filed 2025-02-10.
        rev.append(
            fact(
                date(2024, 1, 1),
                date(2024, 12, 31),
                sum(revenue[q] for q in Q_ENDS[:4]),
                date(2025, 2, 10),
                form="10-K",
                fy=2024,
                fp="FY",
            )
        )
        e.append(
            fact(
                date(2024, 1, 1),
                date(2024, 12, 31),
                sum(eps[q] for q in Q_ENDS[:4]),
                date(2025, 2, 10),
                form="10-K",
                fy=2024,
                fp="FY",
            )
        )
        out = {q.period_end: q for q in quarters(facts(rev, e))}
        q4 = out[date(2024, 12, 31)]
        assert (q4.fiscal_year, q4.fiscal_quarter, q4.form, q4.filed) == (
            2024,
            4,
            "10-K",
            date(2025, 2, 10),
        )
        assert q4.revenue == 130
        assert q4.eps == pytest.approx(1.3)
        latest = out[date(2025, 9, 30)]
        assert latest.revenue_growth == pytest.approx(160 / 120 - 1)
        previous = out[date(2025, 6, 30)]
        assert previous.revenue_growth == pytest.approx(150 / 110 - 1)
        assert latest.revenue_acceleration == pytest.approx((160 / 120 - 1) - (150 / 110 - 1))
        assert out[date(2024, 3, 31)].revenue_growth is None  # no year-ago quarter

    def test_first_filed_value_is_kept(self) -> None:
        end = date(2025, 3, 31)
        rev = [
            fact(date(2025, 1, 1), end, 100.0, date(2025, 5, 1), fy=2025, fp="Q1"),
            fact(date(2025, 1, 1), end, 90.0, date(2026, 5, 1), fy=2026, fp="Q1"),  # restated
        ]
        [q] = quarters(facts(rev, []))
        assert (q.revenue, q.filed, q.fiscal_year) == (100, date(2025, 5, 1), 2025)

    def test_tags_merge_by_period(self) -> None:
        old = fact(date(2017, 1, 1), date(2017, 3, 31), 50.0, date(2017, 5, 1), fy=2017, fp="Q1")
        new = fact(date(2018, 1, 1), date(2018, 3, 31), 60.0, date(2018, 5, 1), fy=2018, fp="Q1")
        cf = CompanyFacts(
            1,
            "Co",
            {
                "us-gaap": {
                    "SalesRevenueNet": {"USD": (old,)},
                    "RevenueFromContractWithCustomerExcludingAssessedTax": {"USD": (new,)},
                }
            },
        )
        out = quarters(cf)
        assert [q.revenue for q in out] == [50, 60]
        assert out[1].revenue_growth == pytest.approx(0.2)

    def test_negative_base_has_no_growth(self) -> None:
        e = [
            fact(date(2024, 1, 1), date(2024, 3, 31), -0.5, date(2024, 5, 1), fy=2024),
            fact(date(2025, 1, 1), date(2025, 3, 31), 0.4, date(2025, 5, 1), fy=2025),
        ]
        out = quarters(facts([], e))
        assert out[1].eps_growth is None

    def test_late_early_and_annual_only_filings_are_skipped(self) -> None:
        late = fact(date(2015, 1, 1), date(2015, 3, 31), 10.0, date(2016, 5, 1), fy=2016)
        annual = fact(
            date(2024, 1, 1), date(2024, 12, 31), 400.0, date(2025, 3, 1), form="10-K", fp="FY"
        )
        # Filed before the quarter even ended: a mis-tagged fact.
        early = fact(date(2018, 7, 1), date(2018, 9, 30), 5.0, date(2018, 6, 28), fy=2018, fp="Q3")
        assert quarters(facts([late, annual, early], [])) == []


# --- the step ----------------------------------------------------------------------------


@dataclass
class FakeFacts:
    by_cik: dict[int, CompanyFacts | None]
    calls: list[int] = field(default_factory=list)

    def company_facts(self, cik: int) -> CompanyFacts | None:
        self.calls.append(cik)
        if cik == 666:
            raise RuntimeError("edgar down")
        return self.by_cik.get(cik)


@pytest.fixture
def calendar(pg_engine: Engine) -> MarketCalendar:
    return sync_calendar(pg_engine)


def bar_tonight(conn: object, ticker_id: int) -> None:
    conn.execute(  # type: ignore[attr-defined]
        insert(DailyBar).values(
            ticker_id=ticker_id,
            session_date=THU,
            open=1,
            high=1,
            low=1,
            close=1,
            raw_close=1,
            volume=1,
        )
    )


def test_step_refreshes_recent_reporters_first_and_caps(
    pg_engine: Engine, calendar: MarketCalendar
) -> None:
    revenue = {e: 100.0 + 10 * i for i, e in enumerate(Q_ENDS)}
    company = facts(quarter_facts(revenue), [])
    with pg_engine.begin() as conn:
        ids = [add_ticker(conn, f"T{i}", cik=100 + i) for i in range(3)]
        broken = add_ticker(conn, "BRK", cik=666)
        for t in [*ids, broken]:
            bar_tonight(conn, t)
        conn.execute(
            insert(EarningsEvent).values(ticker_id=ids[2], report_date=THU, source="FINNHUB")
        )
    risk = RISK.model_copy(
        update={
            "ingest": RISK.ingest.model_copy(
                update={
                    "fundamentals_refresh_sessions": 1,
                    "fundamentals_max_requests": 3,
                    "fundamentals_quarters": 4,
                }
            )
        }
    )
    source = FakeFacts({100: company, 101: None, 102: company})
    with pg_engine.begin() as conn:
        report = ingest_fundamentals(conn, source, calendar, risk, THU)
    # Fetched concurrently, so in any order: the reporter is in, the 4th (666) over the cap.
    assert sorted(source.calls) == [100, 101, 102]
    assert (report.due, report.fetched, report.backlog) == (4, 3, 1)
    assert report.no_facts == ["T1"]
    with pg_engine.connect() as conn:
        rows = conn.execute(select(Fundamental.ticker_id, Fundamental.period_end)).all()
    assert sorted(r.period_end for r in rows if r.ticker_id == ids[0]) == [
        date(2024, 9, 30),
        date(2025, 3, 31),
        date(2025, 6, 30),
        date(2025, 9, 30),
    ]  # the last 4 quarters with a filing (Q4 has no 10-K here)
    with pg_engine.begin() as conn:
        again = ingest_fundamentals(conn, source, calendar, risk, THU)
    assert again.rows_written == 0


def test_step_holds_only_a_few_company_facts_at_once(
    pg_engine: Engine, calendar: MarketCalendar, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Company facts are megabytes each; holding a whole batch ran out of Lambda's memory.
    # At most one group (2 x http_workers) may be fetched and not yet reduced to quarters.
    company = facts(quarter_facts(dict.fromkeys(Q_ENDS, 100.0)), [])
    with pg_engine.begin() as conn:
        for i in range(20):
            bar_tonight(conn, add_ticker(conn, f"F{i}", cik=200 + i))
    risk = RISK.model_copy(
        update={
            "ingest": RISK.ingest.model_copy(
                update={"fundamentals_refresh_sessions": 1, "fundamentals_max_requests": 50}
            )
        }
    )
    held = {"fetched": 0, "reduced": 0, "max": 0}

    class Counting(FakeFacts):
        def company_facts(self, cik: int) -> CompanyFacts | None:
            held["fetched"] += 1
            held["max"] = max(held["max"], held["fetched"] - held["reduced"])
            return super().company_facts(cik)

    real = quarters

    def counting_quarters(f: CompanyFacts) -> list[Quarter]:
        held["reduced"] += 1
        return real(f)

    monkeypatch.setattr("auratrade.ingest.fundamentals.quarters", counting_quarters)
    with pg_engine.begin() as conn:
        report = ingest_fundamentals(
            conn, Counting({200 + i: company for i in range(20)}), calendar, risk, THU
        )
    assert report.fetched == 20
    assert held["reduced"] == 20
    assert held["max"] <= 2 * risk.ingest.http_workers


def test_write_quarters_over_the_parameter_limit(pg_engine: Engine) -> None:
    # 5,100 rows x 13 columns is past Postgres's 65,535 parameters per statement (the backfill
    # writes ~45k rows at once).
    with pg_engine.begin() as conn:
        ticker = add_ticker(conn, "BIG")
    ends = [date(2000, 1, 1) + timedelta(days=i) for i in range(5100)]
    rows = [Quarter(e, e.year, 1, e + timedelta(days=30), "10-Q", f"a{e}", 1.0, 100) for e in ends]
    with pg_engine.begin() as conn:
        assert write_quarters(conn, {ticker: rows}) == 5100
    with pg_engine.begin() as conn:
        assert write_quarters(conn, {ticker: rows}) == 0
        count = conn.execute(select(Fundamental.id).where(Fundamental.ticker_id == ticker)).all()
        assert len(count) == 5100
