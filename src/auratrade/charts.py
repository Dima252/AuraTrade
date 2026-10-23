"""The Analyst's charts (README §5.3 "Charts", §9; task 3.4): a daily and a weekly PNG per
candidate, written to object storage.

- **Daily:** the last ``daily_sessions`` (1 year), candles with the 10-day SMA, 21-day EMA and
  50/150/200-day SMAs, the pivot (and the structural stop) as lines, volume, and the RS line
  (close / SPY) in its own panel.
- **Weekly:** the last ``weekly_weeks`` (2 years) of weekly bars with the 10- and 40-week SMAs
  and volume. A week is the sessions of one ISO calendar week.

Averages are computed over the whole history passed in and then cut to the window, so the
200-day is defined from the chart's first bar. Rendering is headless (matplotlib's Agg backend);
in Lambda, ``MPLCONFIGDIR`` must point at a writable directory (``/tmp``).
"""

import io
from collections.abc import Sequence
from datetime import date

import matplotlib

matplotlib.use("Agg")  # headless; before pyplot and mplfinance are imported

import mplfinance as mpf
import pandas as pd
from matplotlib import pyplot as plt

from auratrade.config import AverageKind, ChartsConfig, TrailSpec
from auratrade.indicators.bars import OHLCV, Bar
from auratrade.indicators.moving_averages import ema, sma
from auratrade.indicators.series import Series
from auratrade.storage import ObjectStore

# Prices on the left: at these sizes a right-hand axis is clipped by the tight layout.
_STYLE = mpf.make_mpf_style(base_mpf_style="yahoo", y_on_right=False, rc={"font.size": 8})
_MA_COLOURS = ("#1f77b4", "#ff7f0e", "#2ca02c", "#9467bd", "#8c564b")


def weekly_bars(dates: Sequence[date], bars: Sequence[OHLCV]) -> tuple[list[date], list[Bar]]:
    """Daily bars grouped by ISO calendar week, dated by each week's last session."""
    out_dates: list[date] = []
    out: list[Bar] = []
    week: tuple[int, int] | None = None
    for day, b in zip(dates, bars, strict=True):
        key = day.isocalendar()[:2]
        if key != week:
            week = key
            out_dates.append(day)
            out.append(Bar(b.open, b.high, b.low, b.close, b.volume))
            continue
        last = out[-1]
        out_dates[-1] = day
        out[-1] = Bar(
            last.open,
            max(last.high, b.high),
            min(last.low, b.low),
            b.close,
            last.volume + b.volume,
        )
    return out_dates, out


def _frame(dates: Sequence[date], bars: Sequence[OHLCV]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Open": [b.open for b in bars],
            "High": [b.high for b in bars],
            "Low": [b.low for b in bars],
            "Close": [b.close for b in bars],
            "Volume": [b.volume for b in bars],
        },
        index=pd.DatetimeIndex(pd.to_datetime(list(dates)), name="Date"),
    )


def _average(closes: Sequence[float], spec: TrailSpec) -> Series:
    return (sma if spec.kind is AverageKind.SMA else ema)(closes, spec.window)


def _render(
    frame: pd.DataFrame,
    *,
    title: str,
    size: tuple[int, int],
    dpi: int,
    averages: Sequence[tuple[str, Series]],
    lines: Sequence[tuple[float, str]],
    rs_line: Series | None,
) -> bytes:
    plots = []
    for (label, values), colour in zip(averages, _MA_COLOURS, strict=False):
        column = pd.Series(values, index=frame.index, dtype="float64")
        if column.notna().any():
            plots.append(mpf.make_addplot(column, color=colour, width=0.8, label=label))
    if rs_line is not None:
        column = pd.Series(rs_line, index=frame.index, dtype="float64")
        if column.notna().any():
            plots.append(
                mpf.make_addplot(column, panel=2, color="#444444", width=0.8, ylabel="RS line")
            )
    extra: dict[str, object] = {}
    if lines:
        extra["hlines"] = {
            "hlines": [level for level, _ in lines],
            "colors": [colour for _, colour in lines],
            "linestyle": "--",
            "linewidths": 1.2,
        }
    figure, _ = mpf.plot(
        frame,
        type="candle",
        style=_STYLE,
        volume=True,
        addplot=plots,
        title=title,
        figsize=(size[0] / dpi, size[1] / dpi),
        panel_ratios=(4, 1, 1) if rs_line is not None else (4, 1),
        returnfig=True,
        tight_layout=True,
        datetime_format="%b %y",
        xrotation=0,
        warn_too_much_data=10_000,
        **extra,
    )
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", dpi=dpi)
    plt.close(figure)
    return buffer.getvalue()


def daily_chart(
    symbol: str,
    dates: Sequence[date],
    bars: Sequence[OHLCV],
    config: ChartsConfig,
    *,
    benchmark_closes: Sequence[float] | None = None,
    pivot: float | None = None,
    stop: float | None = None,
) -> bytes:
    """The daily PNG for the last ``daily_sessions`` of ``bars`` (oldest first)."""
    if not bars:
        raise ValueError("no bars to chart")
    n = min(config.daily_sessions, len(bars))
    closes = [b.close for b in bars]
    averages = [(name, _average(closes, spec)[-n:]) for name, spec in config.daily_averages.items()]
    rs_line: Series | None = None
    if benchmark_closes is not None:
        rs_line = [c / s if s else None for c, s in zip(closes, benchmark_closes, strict=True)][-n:]
    lines = [(level, colour) for level, colour in ((pivot, "#d62728"), (stop, "#7f7f7f")) if level]
    return _render(
        _frame(dates[-n:], bars[-n:]),
        title=f"{symbol} daily",
        size=(config.daily_width, config.daily_height),
        dpi=config.dpi,
        averages=averages,
        lines=lines,
        rs_line=rs_line,
    )


def weekly_chart(
    symbol: str, dates: Sequence[date], bars: Sequence[OHLCV], config: ChartsConfig
) -> bytes:
    """The weekly PNG for the last ``weekly_weeks`` weeks of ``bars``."""
    if not bars:
        raise ValueError("no bars to chart")
    week_dates, weeks = weekly_bars(dates, bars)
    n = min(config.weekly_weeks, len(weeks))
    closes = [b.close for b in weeks]
    averages = [
        (name, _average(closes, spec)[-n:]) for name, spec in config.weekly_averages.items()
    ]
    return _render(
        _frame(week_dates[-n:], weeks[-n:]),
        title=f"{symbol} weekly",
        size=(config.weekly_width, config.weekly_height),
        dpi=config.dpi,
        averages=averages,
        lines=(),
        rs_line=None,
    )


def chart_key(session: date, symbol: str, kind: str) -> str:
    """Where a chart is stored: ``charts/<session>/<symbol>-<daily|weekly>.png``."""
    return f"charts/{session.isoformat()}/{symbol}-{kind}.png"


def store_charts(
    store: ObjectStore, session: date, symbol: str, daily: bytes, weekly: bytes
) -> tuple[str, str]:
    keys = (chart_key(session, symbol, "daily"), chart_key(session, symbol, "weekly"))
    for key, data in zip(keys, (daily, weekly), strict=True):
        store.put(key, data, content_type="image/png")
    return keys
