"""A month replayed end to end in `dry_run` (task 4.3's acceptance): `allocate` on R, the
Portfolio Manager on E, `simulate` each evening, the next R and E, on the test Postgres with
deterministic fund bars and Claude's responses in the API's wire format (no request leaves
the machine)."""

from __future__ import annotations

import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest
import structlog
from sqlalchemy import Engine, insert, orm, select, update

from auratrade.calendar.sessions import load_calendar
from auratrade.config import Settings, TradingMode, load_risk_config
from auratrade.data.alpaca_data import Adjustment, DailyBar
from auratrade.data.news import NewsItem
from auratrade.db.flags import HALT_DRAWDOWN, TRADING_ENABLED, read_flags
from auratrade.db.models import (
    AccountSnapshot,
    BookMark,
    LlmDecision,
    MarketSession,
    PortfolioJournal,
    PortfolioTarget,
    PromptVersion,
    SystemFlag,
)
from auratrade.jobs import allocate as allocate_job
from auratrade.jobs import portfolio_manager as pm_job
from auratrade.jobs.base import NY, JobContext
from auratrade.journal.decisions import deadline, is_execution, news_window
from auratrade.journal.portfolio import simulate_books
from auratrade.llm.portfolio_facts import Event, EventKind, facts_from_json
from tests.llm.test_client import Api, client, fixture
from tests.llm.test_portfolio_manager import answer, weight

RISK = load_risk_config()
R_OCT, E_NOV = date(2026, 10, 30), date(2026, 11, 2)
R_NOV, E_DEC = date(2026, 11, 30), date(2026, 12, 1)
FIRST = date(2025, 1, 1)
LAST = date(2027, 1, 29)


def weekdays(first: date, last: date) -> list[date]:
    out, day = [], first
    while day <= last:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


DAYS = weekdays(FIRST, LAST)
# Each fund's daily drift; SPY's total return adds a 0.4% dividend each quarter's third Friday.
DRIFT = {"SPY": 0.0005, "SSO": 0.001, "BIL": 0.0001, "GLD": 0.0007, "IEF": 0.0001}


@dataclass
class Bars:
    """Deterministic split and total-return bars for every fund on the ETF list."""

    calls: list[tuple[date, date, Adjustment]] = field(default_factory=list)

    def daily_bars(
        self, symbols: Sequence[str], start: date, end: date, *, adjustment: Adjustment = ...
    ) -> dict[str, list[DailyBar]]:
        assert isinstance(adjustment, Adjustment)
        self.calls.append((start, end, adjustment))
        out = {}
        for symbol in symbols:
            drift = DRIFT.get(symbol, 0.0003)
            price = tr = 100.0
            bars = []
            for i, day in enumerate(DAYS):
                move = drift + 0.004 * math.sin(i * 0.7 + len(symbol))
                price *= 1 + move
                tr *= 1 + move
                if (
                    symbol == "SPY"
                    and day.month % 3 == 0
                    and day.weekday() == 4
                    and 15 <= day.day <= 21
                ):
                    tr *= 1.004
                if start <= day <= end:
                    close = tr if adjustment is Adjustment.ALL else price
                    bars.append(DailyBar(symbol, day, close, close, close, close, 1_000_000))
            out[symbol] = bars
        return out


@dataclass
class News:
    def news(self, symbols: Sequence[str] | None, start: datetime, end: datetime) -> list[NewsItem]:
        at = datetime(2026, 10, 30, 21, 0, tzinfo=UTC)
        return [NewsItem(7, "Gold demand rises", "s", "benzinga", "u", ("GLD",), at, at, None)]


@dataclass
class Alerts:
    texts: list[str] = field(default_factory=list)

    def alert(self, text: str) -> None:
        self.texts.append(text)


@pytest.fixture
def world(pg_engine: Engine) -> Engine:
    rows = [
        {
            "session_date": d,
            "open_at": datetime.combine(d, time(9, 30), tzinfo=NY),
            "close_at": datetime.combine(d, time(16, 0), tzinfo=NY),
            "is_early_close": False,
            "minutes": 390,
            "data_status": "COMPLETE",
            "calendar_check": "AGREE",
        }
        for d in DAYS
    ]
    with pg_engine.begin() as conn:
        conn.execute(insert(MarketSession), rows)
        conn.execute(
            update(SystemFlag).where(SystemFlag.name == TRADING_ENABLED).values(value=True)
        )
    return pg_engine


def context(job: str, session: date, mode: TradingMode = TradingMode.DRY_RUN) -> JobContext:
    settings = Settings(_env_file=None, trading_mode=mode)  # type: ignore[call-arg]
    return JobContext(
        job=job,
        session_date=session,
        run_id=uuid.uuid4().hex,
        trading_mode=mode,
        settings=settings,
        risk=RISK,
        storage=None,  # type: ignore[arg-type]  # these jobs store nothing
        started_at=datetime.now(UTC),
        log=structlog.get_logger(job),
    )


def allocate(
    engine: Engine, bars: Bars, session: date, mode: TradingMode = TradingMode.DRY_RUN
) -> dict[str, Any]:
    spec = allocate_job.make_job(engine_factory=lambda _s: engine, funds_factory=lambda _s: bars)
    return dict(spec.run(context("allocate", session, mode)) or {})


def decide(
    engine: Engine,
    bars: Bars,
    session: date,
    *responses: Any,
    event: Event | None = None,
    alerts: Alerts | None = None,
) -> dict[str, Any]:
    api = Api(*responses)
    at = datetime.combine(session, time(8, 45), tzinfo=NY)
    spec = pm_job.make_job(
        event=event,
        engine_factory=lambda _s: engine,
        client_factory=lambda _s, _c: client(api, now=lambda: at),
        funds_factory=lambda _s: bars,
        news_factory=lambda _s: News(),
        alerter_factory=lambda _s: alerts or Alerts(),
        now=lambda: at,
    )
    return dict(spec.run(context("portfolio_manager", session)) or {})


def evening(engine: Engine, bars: Bars, session: date) -> dict[str, Any]:
    with engine.begin() as conn:
        return simulate_books(conn, load_calendar(conn), RISK, bars, session)


def targets(engine: Engine) -> list[PortfolioTarget]:
    with engine.connect() as conn:
        return list(orm.Session(conn).scalars(select(PortfolioTarget).order_by(PortfolioTarget.id)))


def mark(engine: Engine, book: str, day: date) -> BookMark:
    with engine.connect() as conn:
        return (
            orm.Session(conn)
            .scalars(select(BookMark).where(BookMark.book == book, BookMark.session_date == day))
            .one()
        )


def test_a_month_end_to_end(world: Engine) -> None:
    bars = Bars()

    # R: the trial starts; the rule level, the twin's targets and the facts for E.
    out = allocate(world, bars, R_OCT)
    assert out == {
        "ranking": "2026-10-30",
        "execution": "2026-11-02",
        "trend": "UP",
        "capped": False,
        "halted": False,
        "rule_level": RISK.portfolio.up_level,
        "trial_start": "2026-10-30",
    }
    (row,) = targets(world)
    assert row.status == "ALLOCATED"
    assert row.twin_targets == pytest.approx({"SPY": 0.7, "SSO": 0.3})
    facts = facts_from_json(row.facts)
    assert facts.account.equity == RISK.portfolio_manager.dry_run_equity
    assert facts.account.weights == {"BIL": pytest.approx(1.0)}  # cash before the first E
    assert {e.symbol for e in facts.etfs} == set(RISK.portfolio.satellite_list)
    assert facts.notes
    assert "dry_run" in facts.notes[0]
    assert evening(world, bars, R_OCT) == {
        "TWIN": {"sessions": 1, "equity": 100_000.0},
        "SIM": {"sessions": 1, "equity": 100_000.0},
        "SPY": {"sessions": 1, "equity": 100_000.0},
        "scorecard": {"through": "2026-10-30", "months_complete": 0, "excess_after_tax": 0.0},
    }

    # E: the decision, through the guardrails, recorded.
    tilt = answer([weight("GLD", 0.10, 0.01), weight("SPY", 0.10)], core_shift=0.1)
    out = decide(world, bars, E_NOV, tilt)
    assert out["fell_back"] is None
    assert out["satellite"] == {"GLD": 0.1, "SPY": 0.1}
    assert out["book_level"] == pytest.approx(1.4)
    assert out["increases_allowed"] is True
    (row,) = targets(world)
    assert row.status == "DECIDED"
    assert row.satellite == pytest.approx({"GLD": 0.1, "SPY": 0.1})
    assert sum(row.targets.values()) == pytest.approx(1.0)
    assert row.satellite_tax == 0.0
    assert decide(world, bars, E_NOV) == {"skipped": "already decided"}

    # The evenings: the book holds gold, the twin doesn't.
    for day in weekdays(E_NOV, date(2026, 11, 27)):
        evening(world, bars, day)
    sim, twin = mark(world, "SIM", E_NOV), mark(world, "TWIN", E_NOV)
    assert {t["symbol"] for t in sim.trades} == {"SPY", "SSO", "GLD"}
    assert "GLD" not in twin.holdings
    assert sim.holdings["GLD"]["value"] == pytest.approx(10_000, rel=0.01)

    # The next R: the account is the book; last month's satellite is the previous.
    allocate(world, bars, R_NOV)
    nov = facts_from_json(targets(world)[1].facts)
    assert nov.account.previous_satellite == pytest.approx({"GLD": 0.1, "SPY": 0.1})
    assert nov.account.weights["GLD"] == pytest.approx(0.1, abs=0.01)
    assert nov.account.lots["GLD"][0].opened == E_NOV
    assert [d.session for d in nov.history] == [E_NOV]
    assert nov.history[0].summary == "A quiet month."
    # The month is still open at R: its excess to date, and the scorecard so far.
    assert nov.history[0].excess_vs_twin is not None
    assert nov.scorecard is not None
    assert nov.scorecard["months_complete"] == 0
    assert nov.scorecard["expectations_scored"] == 0
    # From the trial's R, where the month's excess runs from its E.
    assert "excess_after_tax" in nov.scorecard

    # The next E: a hold.
    out = decide(world, bars, E_DEC, answer(None))
    assert out["held_satellite"] is True
    assert out["book_level"] == pytest.approx(RISK.portfolio.up_level)
    dec = targets(world)[1]
    assert dec.held_satellite
    assert dec.satellite == pytest.approx({"GLD": 0.1, "SPY": 0.1})
    evening(world, bars, E_DEC)
    # Only the core moved back to the rule level; the satellite was left alone.
    traded = {t["symbol"] for t in mark(world, "SIM", E_DEC).trades}
    assert "GLD" not in traded

    # The scorecard: November's decision is scored to December's E against the twin and SPY.
    with world.connect() as conn:
        rows = orm.Session(conn).scalars(select(PortfolioJournal)).all()
    by_e = {j.execution_session: j for j in rows}
    nov_r, dec_r = by_e[E_NOV].results, by_e[E_DEC].results
    assert nov_r["complete"] is True
    assert nov_r["end"] == str(E_DEC)
    assert dec_r["complete"] is False
    assert {t["symbol"] for t in by_e[E_NOV].trades} == {"SPY", "SSO", "GLD"}
    tr = {
        s: {b.session: b.close for b in v}
        for s, v in bars.daily_bars(
            ["GLD", "SPY", "BIL"], E_NOV, E_DEC, adjustment=Adjustment.ALL
        ).items()
    }
    r = {s: tr[s][E_DEC] / tr[s][E_NOV] - 1 for s in tr}
    assert nov_r["tilts"] == {"GLD": pytest.approx(0.1 * (r["GLD"] - r["SPY"]))}
    assert nov_r["shift_score"] == pytest.approx(0.1 * (r["SPY"] - r["BIL"]))
    (expectation,) = nov_r["expectations"]
    assert expectation["symbol"] == "GLD"
    assert expectation["expected"] == 0.01
    assert expectation["actual"] == pytest.approx(r["GLD"] - r["SPY"])
    assert dec_r["expectations"] == []  # open, and a hold states none
    sim_b, twin_b = mark(world, "SIM", E_NOV), mark(world, "TWIN", E_NOV)
    sim_e, twin_e = mark(world, "SIM", E_DEC), mark(world, "TWIN", E_DEC)
    after = (sim_e.equity - sim_e.tax_owed) / (sim_b.equity - sim_b.tax_owed) - (
        twin_e.equity - twin_e.tax_owed
    ) / (twin_b.equity - twin_b.tax_owed)
    assert nov_r["after_tax"]["excess"] == pytest.approx(after)
    assert sim_e.tax_owed > 0  # gains and a dividend owe tax if sold
    assert mark(world, "SPY", E_DEC).holdings.keys() == {"SPY"}

    with world.connect() as conn:
        assert conn.scalar(select(PromptVersion.id).limit(1)).startswith("pm-2-")
        decisions = orm.Session(conn).scalars(select(LlmDecision).order_by(LlmDecision.id)).all()
        journal = orm.Session(conn).scalars(select(PortfolioJournal)).all()
    assert [d.failure for d in decisions] == [None, None]
    assert decisions[0].answer["satellite"][0]["symbol"] == "GLD"
    assert decisions[0].model == "claude-opus-5-5"
    assert decisions[0].cost_usd > 0
    assert len(decisions[0].turns) == 1
    assert sorted(j.execution_session for j in journal) == [E_NOV, E_DEC]
    # Every fetch counts sessions back from the trial's start, never past T.
    assert all(end <= E_DEC for _start, end, _adj in bars.calls)


def test_a_failure_holds_and_alerts(world: Engine) -> None:
    bars, alerts = Bars(), Alerts()
    allocate(world, bars, R_OCT)
    out = decide(world, bars, E_NOV, fixture("refusal"), alerts=alerts)
    assert out["fell_back"] == "REFUSAL"
    # No previous satellite: the neutral, so the book is the twin.
    assert out["satellite"] == {"SPY": RISK.portfolio.satellite_share}
    (row,) = targets(world)
    assert row.fell_back
    assert row.targets == pytest.approx(row.twin_targets)
    (text,) = alerts.texts
    assert "the LLM failed (REFUSAL)" in text
    with world.connect() as conn:
        (journal,) = orm.Session(conn).scalars(select(PortfolioJournal)).all()
    assert journal.summary == "Held: the LLM failed (REFUSAL)."


def test_the_kill_switch_blocks_the_first_purchases(world: Engine) -> None:
    bars = Bars()
    with world.begin() as conn:
        conn.execute(
            update(SystemFlag).where(SystemFlag.name == TRADING_ENABLED).values(value=False)
        )
    allocate(world, bars, R_OCT)
    out = decide(world, bars, E_NOV, answer([weight("GLD", 0.10), weight("SPY", 0.10)]))
    assert out["increases_allowed"] is False
    assert "NO_INCREASE" in out["clips"]
    (row,) = targets(world)
    assert row.targets == pytest.approx({"BIL": 1.0})


def test_an_event_decides_at_once_and_only_reduces_risk(world: Engine) -> None:
    bars = Bars()
    allocate(world, bars, R_OCT)
    decide(world, bars, E_NOV, answer([weight("GLD", 0.10), weight("SPY", 0.10)]))
    for day in weekdays(E_NOV, date(2026, 11, 11)):
        evening(world, bars, day)
    event = Event(EventKind.HALT, "Equity closed at 74.9% of its peak.")
    out = decide(
        world,
        bars,
        date(2026, 11, 12),
        answer([weight("QQQ", 0.10), weight("GLD", 0.10)], core_shift=0.2),
        event=event,
    )
    assert out["increases_allowed"] is False
    assert out["book_level"] <= RISK.portfolio.capped_level
    rows = targets(world)
    assert [r.kind for r in rows] == ["REBALANCE", "EVENT"]
    event_row = rows[1]
    assert event_row.status == "DECIDED"
    assert event_row.halted
    assert event_row.targets.get("QQQ", 0.0) == 0.0
    assert event_row.targets.get("SSO", 0.0) == 0.0
    assert facts_from_json(event_row.facts).event == event


def test_the_skips(world: Engine) -> None:
    bars = Bars()
    assert allocate(world, bars, date(2026, 10, 29)) == {"skipped": "not a ranking session"}
    assert decide(world, bars, date(2026, 11, 3)) == {"skipped": "not an execution session"}
    with pytest.raises(RuntimeError, match="allocate didn't run"):
        decide(world, bars, E_NOV)
    assert evening(world, bars, R_OCT) == {"skipped": "the trial hasn't started"}
    with world.begin() as conn:
        conn.execute(
            update(MarketSession)
            .where(MarketSession.session_date == R_OCT)
            .values(data_status="INCOMPLETE")
        )
        calendar = load_calendar(conn)
    assert allocate(world, bars, R_OCT) == {"skipped": "data status INCOMPLETE"}
    assert is_execution(calendar, E_NOV)
    assert not is_execution(calendar, date(2026, 11, 3))


def test_the_deadline_applies_on_e_and_not_to_a_replay() -> None:
    morning = datetime.combine(E_NOV, time(8, 45), tzinfo=NY)
    assert deadline(RISK, E_NOV, morning) == datetime.combine(E_NOV, time(12), tzinfo=NY)
    assert deadline(RISK, E_NOV, morning + timedelta(days=3)) is None


def test_a_late_answer_on_e_holds(world: Engine) -> None:
    bars = Bars()
    allocate(world, bars, R_OCT)
    api = Api(answer([weight("GLD", 0.10), weight("SPY", 0.10)]))
    late = datetime.combine(E_NOV, time(12, 30), tzinfo=NY)
    spec = pm_job.make_job(
        engine_factory=lambda _s: world,
        client_factory=lambda _s, _c: client(api, now=lambda: late),
        funds_factory=lambda _s: bars,
        news_factory=lambda _s: News(),
        alerter_factory=lambda _s: Alerts(),
        now=lambda: late,
    )
    out = dict(spec.run(context("portfolio_manager", E_NOV)) or {})
    assert out["fell_back"] == "DEADLINE"
    assert api.requests == []  # no request starts after the deadline


def test_a_replay_reads_no_news_after_e(world: Engine) -> None:
    with world.connect() as conn:
        calendar = load_calendar(conn)
    open_e = datetime.combine(E_NOV, time(9, 30), tzinfo=NY)
    later = open_e + timedelta(days=20)
    start, end = news_window(calendar, RISK, E_NOV, later)
    assert end == open_e
    assert start == datetime.combine(date(2026, 10, 26), time(9, 30), tzinfo=NY)
    morning = datetime.combine(E_NOV, time(8, 45), tzinfo=NY)
    assert news_window(calendar, RISK, E_NOV, morning)[1] == morning


def halt_flag(engine: Engine, on: bool, peak: float) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(SystemFlag)
            .where(SystemFlag.name == HALT_DRAWDOWN)
            .values(value=on, details={"peak": peak})
        )


def account_snapshot(engine: Engine, day: date, equity: float) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(AccountSnapshot).values(
                session_date=day,
                equity=equity,
                cash=equity,
                long_market_value=0.0,
                peak_equity=equity,
                drawdown=0.0,
                open_positions=0,
            )
        )


@pytest.mark.parametrize(
    ("equity", "halted", "level"),
    [(100_000.0, True, RISK.portfolio.capped_level), (250_000.0, False, RISK.portfolio.up_level)],
)
def test_on_paper_r_reads_the_accounts_halt_and_may_turn_it_off(
    world: Engine, equity: float, halted: bool, level: float
) -> None:
    halt_flag(world, True, 200_000.0)
    account_snapshot(world, R_OCT, equity)
    out = allocate(world, Bars(), R_OCT, TradingMode.PAPER)
    assert (out["halted"], out["rule_level"]) == (halted, level)
    assert out["halt"]["turned_off"] is not halted
    (row,) = targets(world)
    assert (row.halted, row.rule_level) == (halted, level)
    assert facts_from_json(row.facts).trend.halted is halted
    with world.connect() as conn:
        assert read_flags(conn).halt_drawdown is halted


def test_in_dry_run_r_uses_the_simulated_books_halt(world: Engine) -> None:
    halt_flag(world, True, 200_000.0)
    out = allocate(world, Bars(), R_OCT)
    assert (out["halted"], out["rule_level"]) == (False, RISK.portfolio.up_level)
    assert "halt" not in out
    with world.connect() as conn:
        assert read_flags(conn).halt_drawdown is True  # the account's flag is left alone
