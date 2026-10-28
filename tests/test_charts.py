import io
from datetime import date, timedelta

import pytest
from PIL import Image

from auratrade.charts import daily_chart
from auratrade.config import load_risk_config
from auratrade.indicators.bars import Bar

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


def test_daily_chart_size_and_window() -> None:
    days, bars = history(400)
    spy = [400 + 0.05 * i for i in range(400)]
    png = daily_chart("TEST", days, bars, CFG, benchmark_closes=spy)
    assert size(png) == (CFG.daily_width, CFG.daily_height)


def test_daily_chart_without_benchmark_levels_or_full_averages() -> None:
    days, bars = history(60)  # the 150/200-day never fill: those lines are left out
    assert size(daily_chart("TEST", days, bars, CFG)) == (CFG.daily_width, CFG.daily_height)


def test_no_bars_rejects() -> None:
    with pytest.raises(ValueError, match="no bars"):
        daily_chart("TEST", [], [], CFG)
