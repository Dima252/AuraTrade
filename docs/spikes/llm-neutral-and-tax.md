# Finding: the book's neutral wasn't the tested strategy, and nothing held the LLM to holding (2026-10-27)

**Found** in a review of the LLM's role the user asked for after task 4.1. **Approved by the user 2026-10-27** ("i approve your changes"), with an addition: the LLM must understand that trading loses money to tax and prefer to hold. Recorded as decision #163.

## What was expected

The plan (R1, #154) says the twin is "the core rule on the whole account", and everything the LLM does shows up as the book's excess over it. That reads as if the book equals the twin when the LLM changes nothing.

## What the rules actually gave

The book was the core (80%) at the rule level plus the shift, and the satellite (20%) at the LLM's weights, with each ETF capped at 10% and no leverage in the satellite.

| Trend up, the LLM changes nothing | S&P exposure |
|---|---|
| The twin (the strategy that passed the gate, #160) | 1.30x |
| The book, satellite in GLD / bonds / BIL | 0.8 × 1.3 = 1.04x |
| The book, satellite 10% SPY + 10% QQQ | about 1.25x |
| The book, shift +0.2 and satellite 10% SPY + 10% QQQ | about 1.35x |

- **The twin's own mix (70% SPY, 30% SSO) was out of the satellite's reach:** SPY capped at 10%, SSO forbidden. To match the tested strategy, the LLM had to spend its whole upward shift.
- **The cost:** in up-trend months a diversified satellite ran about 0.15-0.25x below the twin, roughly 0.5-1 point a year (an equity premium of about 6% over T-bills, up about 70% of the months). The backtest's edge is +0.5 a year after tax (#160). The scorecard would have charged that structural gap to the LLM.
- **In a down-trend** the satellite could add equity the rules said not to hold: the twin at 0.5x, the book at 0.8 × 0.5 + 20% in QQQ and IWM ≈ 0.6x.
- **Tax:** a changed target is always traded (the drift band only spares unchanged targets), and every sale of a gain realizes 25%. Nothing in code limited the tax the LLM's changes could realize; README §7.4 left it to the prompt.
- **Measurement:** with a tracking error of 3-5% a year against the twin, 12 months can't separate skill from luck: a 1-point edge needs decades to show at two standard errors. The latitude rule at month 12 (B4) can't rest on returns.

## The changes (approved)

1. **The neutral satellite is SPY at 20%** (`portfolio.satellite_neutral`; SPY may fill the whole satellite). **The book's level** is the rule level plus the shift (the band unchanged); code sizes the core so that, with the satellite at its neutral, the book's S&P exposure equals that level: core level = (book level − 0.2) / 0.8. Up-trend: core 1.375x, book 1.3x, holdings 70% SPY and 30% SSO, the same as the twin's. Down-trend: core 0.375x, book 0.5x. Capped or halted: core 1.0x, no SSO. **At neutral the book is the twin**, so the excess over the twin is only what the LLM chose.
2. **The down-trend gap is closed by 1:** the satellite's neutral already counts in the level, so replacing SPY with another equity ETF changes the exposure only by that ETF's beta difference (QQQ, IWM: about +0.03x at 20%). No further rule.
3. **Hold by default, in code** (`portfolio.satellite_min_change`, 2 points of equity): a satellite weight within 2 points of last month's target keeps that target, so it isn't traded. A full exit (weight 0) is always honoured; a new holding below 2 points isn't opened.
4. **A yearly tax budget for the satellite** (`portfolio.satellite_tax_budget`, 0.2% of equity a year): the tax the satellite's sales would realize (25% of their net FIFO gains), with the year's earlier satellite tax, may not exceed it. A decision over the budget is moved back toward last month's targets until it fits. **Not applied while the trend is DOWN, capped or halted**, so reducing risk is never held back by tax.
5. **The prompt (task 4.2), on the user's instruction:** the Portfolio Manager is told that every trade of a gain realizes 25% tax and that holding is the default; the facts show each holding's unrealized gain and the tax a full sale would realize now, and the budget left this year; "hold everything" is an explicit answer; each change states the tax it realizes and why it beats holding.
6. **The month-12 latitude rule (B4)** is pre-registered on process (rule-following, the calibration of its expectations, the tax it realized), with returns reported but not decisive.

## What doesn't change

The backtest and its result (#160): the twin is unchanged, and the simulator never used the book's targets. The rule level, the band, the halt, the stop and the twin.
