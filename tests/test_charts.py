import io
from datetime import date, timedelta
from pathlib import Path

import pytest
from PIL import Image

from auratrade.charts import (
    chart_key,
    daily_chart,
    store_charts,
    weekly_bars,
    weekly_chart,
)
from auratrade.config import load_risk_config
from auratrade.indicators.bars import Bar
from auratrade.storage import LocalStore

CFG = load_risk_config().charts


def weekdays(start: date, n: int) -> list[date]:
    out, day = [], start
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def history(n: int) -> tuple[list[date], list[Bar]]:
    days = weekdays(date(2024, 1, 1), n)
    bars = [
        Bar(
            open=50 + 0.1 * i, high=51 + 0.1 * i, low=49 + 0.1 * i, close=50.5 + 0.1 * i, volume=1e6
        )
        for i in range(n)
    ]
    return days, bars


def size(png: bytes) -> tuple[int, int]:
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    with Image.open(io.BytesIO(png)) as image:
        return image.size


def test_weekly_bars_group_iso_weeks() -> None:
    # Wed 2024-01-03 .. Tue 2024-01-09: one partial week, then Mon-Tue of the next.
    days = weekdays(date(2024, 1, 3), 5)
    bars = [Bar(10 + i, 12 + i, 9 + i, 11 + i, 100) for i in range(5)]
    week_dates, weeks = weekly_bars(days, bars)
    assert week_dates == [date(2024, 1, 5), date(2024, 1, 9)]
    assert weeks[0] == Bar(open=10, high=14, low=9, close=13, volume=300)
    assert weeks[1] == Bar(open=13, high=16, low=12, close=15, volume=200)


def test_weekly_bars_handle_a_holiday_week() -> None:
    # A week with only Thursday-Friday (as after a long holiday) is still one bar.
    days = [date(2024, 1, 4), date(2024, 1, 5), date(2024, 1, 8)]
    bars = [Bar(1, 2, 0.5, 1.5, 10)] * 3
    week_dates, _ = weekly_bars(days, bars)
    assert week_dates == [date(2024, 1, 5), date(2024, 1, 8)]


def test_daily_chart_size_and_window() -> None:
    days, bars = history(400)
    spy = [400 + 0.05 * i for i in range(400)]
    png = daily_chart("TEST", days, bars, CFG, benchmark_closes=spy, pivot=90.0, stop=85.0)
    assert size(png) == (CFG.daily_width, CFG.daily_height)


def test_daily_chart_without_benchmark_levels_or_full_averages() -> None:
    days, bars = history(60)  # the 150/200-day never fill: those lines are left out
    assert size(daily_chart("TEST", days, bars, CFG)) == (CFG.daily_width, CFG.daily_height)


def test_weekly_chart_size() -> None:
    days, bars = history(600)
    assert size(weekly_chart("TEST", days, bars, CFG)) == (CFG.weekly_width, CFG.weekly_height)


def test_no_bars_rejects() -> None:
    with pytest.raises(ValueError, match="no bars"):
        daily_chart("TEST", [], [], CFG)
    with pytest.raises(ValueError, match="no bars"):
        weekly_chart("TEST", [], [], CFG)


def test_charts_are_stored_under_the_session(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    keys = store_charts(store, date(2026, 10, 23), "ABC", b"daily", b"weekly")
    assert keys == (
        "charts/2026-10-23/ABC-daily.png",
        "charts/2026-10-23/ABC-weekly.png",
    )
    assert store.get(keys[0]) == b"daily"
    assert chart_key(date(2026, 10, 23), "ABC", "weekly") == keys[1]
