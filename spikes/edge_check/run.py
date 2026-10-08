"""Spike 0.3h, step 2: a rough, spike-grade edge check on daily bars, 2017-2022 entries only.

    uv run --group spikes python -m spikes.edge_check.run

Tunes nothing: every threshold below is fixed up front from the README's source rules or the
plan's simple versions. NOT a fill model; nothing outside spikes/ may import it (CLAUDE.md §6).
Results: docs/spikes/edge-check.md.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
ENTRY_START, ENTRY_END = "2017-01-01", "2022-12-31"
SLIPPAGE = 0.002  # per side, crude and fixed
MAX_STOP = 0.08  # stop at the base low, capped at 8% below the fill
MAX_CHASE = 1.05  # no entry more than 5% above the pivot (README §7.1)
SPLICE_GAP = 30  # sessions without a traded bar: treat the ticker as a different security
BOOTSTRAP = 2000


@dataclass(frozen=True)
class Trade:
    setup: str
    security: str
    source: str
    signal_date: pd.Timestamp
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry: float
    stop: float
    exit: float
    exit_reason: str
    sessions: int
    spy_above_200: bool

    @property
    def r(self) -> float:
        return (self.exit - self.entry) / (self.entry - self.stop)

    @property
    def ret(self) -> float:
        return self.exit / self.entry - 1


def load() -> tuple[dict[str, pd.DataFrame], dict[str, str], pd.Series]:
    split = pd.read_parquet(DATA / "bars_split.parquet")
    raw = pd.read_parquet(DATA / "bars_raw.parquet")
    df = split.merge(raw, on=["symbol", "date"], how="left")
    df["date"] = pd.to_datetime(df["date"])
    df = df[df["v"] > 0]  # zero-volume bars repeat the last close after a delisting (0.3a)
    sessions = pd.DatetimeIndex(sorted(df.loc[df.symbol == "SPY", "date"].unique()))
    if len(sessions) < 1000:
        sessions = pd.DatetimeIndex(sorted(df["date"].unique()))
    pos = pd.Series(np.arange(len(sessions)), index=sessions)
    df = df[df["date"].isin(sessions)].sort_values(["symbol", "date"])
    # Ticker reuse (0.3a: BBT, HTZ): a long gap starts a new security.
    idx = pos.loc[df["date"]].to_numpy()
    new_seg = np.r_[
        True,
        (df["symbol"].to_numpy()[1:] != df["symbol"].to_numpy()[:-1]) | (np.diff(idx) > SPLICE_GAP),
    ]
    seg = np.cumsum(new_seg)
    first_of_symbol = df.groupby("symbol").cumcount().to_numpy() == 0
    seg_no = (
        pd.Series(seg)
        .groupby(df["symbol"].to_numpy())
        .transform(lambda s: s - s.iloc[0])
        .to_numpy()
    )
    df["security"] = np.where(
        first_of_symbol | (seg_no == 0), df["symbol"], df["symbol"] + "#" + seg_no.astype(str)
    )
    # Keep only securities that ever pass the liquidity filter (and SPY), before going wide.
    df["dv"] = df["c"] * df["v"]
    df["adv50"] = df.groupby("security")["dv"].transform(
        lambda x: x.rolling(50, min_periods=40).mean()
    )
    ever = df.loc[(df["adv50"] >= 10e6) & (df["c_raw"] >= 10), "security"].unique()
    df = df[df["security"].isin(set(ever) | {"SPY"})]
    sources = json.loads((DATA / "sources.json").read_text(encoding="utf-8"))
    sec_source = {sec: sources.get(sec.split("#")[0], "unknown") for sec in df["security"].unique()}
    wide = {
        f: df.pivot(index="date", columns="security", values=f).reindex(sessions)
        for f in ("o", "h", "l", "c", "v", "c_raw")
    }
    spy = wide["c"]["SPY"] if "SPY" in wide["c"] else pd.Series(np.nan, index=sessions)
    return wide, sec_source, spy


def indicators(w: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    c, h, lo, v = w["c"], w["h"], w["l"], w["v"]
    out: dict[str, pd.DataFrame] = {}
    for n in (10, 20, 50, 150, 200):
        out[f"sma{n}"] = c.rolling(n, min_periods=n).mean()
    out["hi252"] = h.rolling(252, min_periods=200).max()
    out["lo252"] = lo.rolling(252, min_periods=200).min()
    for n in (21, 63, 126, 189, 252):
        out[f"r{n}"] = c / c.shift(n) - 1
    out["adv50"] = (c * v).rolling(50, min_periods=40).mean()
    history = c.notna().cumsum()
    out["universe"] = (w["c_raw"] >= 10) & (out["adv50"] >= 10e6) & (history >= 252)
    if "SPY" in out["universe"]:
        out["universe"]["SPY"] = False
    score = 0.4 * out["r63"] + 0.2 * out["r126"] + 0.2 * out["r189"] + 0.2 * out["r252"]
    out["rs"] = score.where(out["universe"]).rank(axis=1, pct=True) * 100
    tt = (
        (c > out["sma150"])
        & (c > out["sma200"])  # 1
        & (out["sma150"] > out["sma200"])  # 2
        & (out["sma200"] > out["sma200"].shift(22))  # 3
        & (out["sma50"] > out["sma150"])
        & (out["sma50"] > out["sma200"])  # 4
        & (c > out["sma50"])  # 5
        & (c >= 1.30 * out["lo252"])  # 6
        & (c >= 0.75 * out["hi252"])  # 7
        & (out["rs"] >= 70)  # 8
    )
    out["tt"] = tt & out["universe"]
    out["pivot10"] = h.rolling(10, min_periods=10).max()
    out["low10"] = lo.rolling(10, min_periods=10).min()

    # VCP-lite: three 10-session segments, each range tighter than the last; the final one
    # <= 10%; base depth <= 35%; volume dries up in the final segment.
    rng = []
    for k in (2, 1, 0):  # oldest segment first
        hk = h.rolling(10, min_periods=10).max().shift(10 * k)
        lk = lo.rolling(10, min_periods=10).min().shift(10 * k)
        rng.append((hk - lk) / hk)
    h30 = h.rolling(30, min_periods=30).max()
    depth = (h30 - lo.rolling(30, min_periods=30).min()) / h30
    dry = v.rolling(10, min_periods=10).mean() < v.rolling(50, min_periods=40).mean().shift(10)
    out["vcp"] = (
        out["tt"]
        & (rng[0] > rng[1])
        & (rng[1] > rng[2])
        & (rng[2] <= 0.10)
        & (depth <= 0.35)
        & dry
        & (c < out["pivot10"])
    )

    # Momentum-lite: top 2% by 1-, 3- or 6-month return; a >= 30% move off the 3-month low;
    # a 10-session consolidation <= 15% deep with higher lows, riding the rising 10/20-day.
    top = pd.DataFrame(False, index=c.index, columns=c.columns)
    for n in (21, 63, 126):
        top |= out[f"r{n}"].where(out["universe"]).rank(axis=1, pct=True) >= 0.98
    move = c / lo.rolling(63, min_periods=50).min() - 1 >= 0.30
    tight = (out["pivot10"] - out["low10"]) / out["pivot10"] <= 0.15
    higher_lows = lo.rolling(5, min_periods=5).min() > lo.rolling(5, min_periods=5).min().shift(5)
    riding = (
        (c > out["sma10"]) & (out["sma10"] > out["sma20"]) & (out["sma20"] > out["sma20"].shift(5))
    )
    out["momentum"] = out["tt"] & top & move & tight & higher_lows & riding & (c < out["pivot10"])
    return out


def simulate(
    setup: str,
    exit_ma: str,
    w: dict[str, pd.DataFrame],
    ind: dict[str, pd.DataFrame],
    sources: dict[str, str],
    spy_above: pd.Series,
) -> list[Trade]:
    trades: list[Trade] = []
    dates = w["c"].index
    in_window = (dates >= ENTRY_START) & (dates <= ENTRY_END)
    signals = ind[setup]
    o, h, lo, c = (w[f].to_numpy() for f in ("o", "h", "l", "c"))
    ma = ind[exit_ma].to_numpy()
    pivot, low10 = ind["pivot10"].to_numpy(), ind["low10"].to_numpy()
    sig = signals.to_numpy() & in_window[:, None]
    n_dates = len(dates)
    for j, sec in enumerate(signals.columns):
        busy_until = -1
        for t in np.flatnonzero(sig[:, j]):
            e = t + 1
            if e <= busy_until or e >= n_dates or np.isnan(o[e, j]):
                continue
            pv = pivot[t, j]
            if h[e, j] < pv or o[e, j] > pv * MAX_CHASE:
                continue  # no break of the pivot, or gapped past the max chase
            fill = max(pv, o[e, j]) * (1 + SLIPPAGE)
            stop = max(low10[t, j], fill * (1 - MAX_STOP))
            if stop >= fill:
                continue
            exit_px, reason, x = np.nan, "", e
            last_traded = e
            for x in range(e, n_dates):
                if np.isnan(c[x, j]):
                    if x - last_traded > SPLICE_GAP:
                        break
                    continue
                last_traded = x
                if lo[x, j] <= stop:  # pessimistic: on the entry day the stop comes first
                    exit_px, reason = min(stop, o[x, j]) * (1 - SLIPPAGE), "stop"
                    break
                if c[x, j] < ma[x, j]:
                    exit_px, reason = c[x, j] * (1 - SLIPPAGE), f"close<{exit_ma}"
                    break
            if not reason:
                x = last_traded
                exit_px = c[x, j] * (1 - SLIPPAGE)
                reason = "delisted/data end" if x < n_dates - 1 else "open at data end"
            busy_until = x
            trades.append(
                Trade(
                    setup=setup,
                    security=sec,
                    source=sources.get(sec, "unknown"),
                    signal_date=dates[t],
                    entry_date=dates[e],
                    exit_date=dates[x],
                    entry=float(fill),
                    stop=float(stop),
                    exit=float(exit_px),
                    exit_reason=reason,
                    sessions=x - e + 1,
                    spy_above_200=bool(spy_above.iloc[e]),
                )
            )
    return trades


def stats(df: pd.DataFrame) -> dict[str, float | int]:
    if df.empty:
        return {"n": 0}
    wins, losses = df[df.r > 0], df[df.r <= 0]
    gross_loss = -losses.ret.sum()
    rng = np.random.default_rng(7)
    by_day = df.groupby("entry_date")["r"].agg(["sum", "count"])
    sums, counts = by_day["sum"].to_numpy(), by_day["count"].to_numpy()
    means = []
    for _ in range(BOOTSTRAP):
        pick = rng.integers(0, len(sums), len(sums))
        means.append(sums[pick].sum() / counts[pick].sum())
    lo, hi = np.percentile(means, [5, 95])
    return {
        "n": len(df),
        "win_rate": round(len(wins) / len(df), 3),
        "avg_r": round(df.r.mean(), 3),
        "ci90_avg_r": (round(float(lo), 3), round(float(hi), 3)),
        "median_r": round(df.r.median(), 3),
        "avg_win_r": round(wins.r.mean(), 2) if len(wins) else 0.0,
        "avg_loss_r": round(losses.r.mean(), 2) if len(losses) else 0.0,
        "avg_ret_pct": round(100 * df.ret.mean(), 2),
        "profit_factor": round(wins.ret.sum() / gross_loss, 2) if gross_loss > 0 else float("inf"),
        "avg_sessions": round(df.sessions.mean(), 1),
    }


def main() -> None:
    w, sources, spy = load()
    ind = indicators(w)
    spy_above = (spy > spy.rolling(200, min_periods=200).mean()).fillna(False)
    universe_daily = ind["universe"].sum(axis=1)
    in_uni = ind["universe"].any(axis=0)
    uni_sources = pd.Series({s: sources.get(s, "unknown") for s in in_uni[in_uni].index})
    print("securities ever in universe:", int(in_uni.sum()), uni_sources.value_counts().to_dict())
    print(
        "universe size by year (median per session):",
        universe_daily.groupby(universe_daily.index.year).median().astype(int).to_dict(),
    )
    trades = simulate("vcp", "sma50", w, ind, sources, spy_above) + simulate(
        "momentum", "sma10", w, ind, sources, spy_above
    )
    df = pd.DataFrame([{**t.__dict__, "r": t.r, "ret": t.ret} for t in trades])
    df.to_parquet(DATA / "trades.parquet", index=False)
    report: dict[str, object] = {}
    for setup, g in df.groupby("setup"):
        report[str(setup)] = {
            "all": stats(g),
            "by_year": {int(y): stats(gy) for y, gy in g.groupby(g.entry_date.dt.year)},
            "spy_above_200": stats(g[g.spy_above_200]),
            "spy_below_200": stats(g[~g.spy_above_200]),
            "by_source": {str(s): stats(gs) for s, gs in g.groupby("source")},
            "exit_reasons": g.exit_reason.value_counts().to_dict(),
        }
    out = json.dumps(report, indent=1, default=str)
    (DATA / "report.json").write_text(out, encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
