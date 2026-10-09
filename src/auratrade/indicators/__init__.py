"""Pure indicator functions on split-adjusted bars.

No I/O and no configuration lookups: every window and threshold is a parameter, supplied by the
caller from ``config/risk.yaml``. Windows count sessions (bars), never calendar days.

- ``series``: rolling helpers and the aligned-series convention
- ``moving_averages``: SMA, EMA, slope, extension above an average
- ``volatility``: ATR, ADR %
- ``ranges``: highest high / lowest low (the 52-week range)
- ``relative_strength``: RS score and rating, RS line and its states, momentum rank, group RS rank
- ``volume``: up/down volume ratio, half-day normalization, intraday pace curve
- ``distribution``: distribution days, rally attempts and follow-through days (per index)
- ``breadth``: share above an average, new highs and lows
- ``earnings``: earnings-gap catalyst
- ``climax``: climax-top signals for held positions
"""
