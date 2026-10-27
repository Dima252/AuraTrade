"""Pure indicator functions on split-adjusted bars (the regime's and the charts').

No I/O and no configuration lookups: every window and threshold is a parameter, supplied by the
caller from ``config/risk.yaml``. Windows count sessions (bars), never calendar days.

- ``series``: rolling helpers and the aligned-series convention
- ``moving_averages``: SMA, EMA, slope, extension above an average
- ``volume``: up/down volume ratio, half-day normalization
- ``distribution``: distribution days, rally attempts and follow-through days (per index)

The setup system's indicators (bases, climax, ranges, relative strength, earnings gaps, breadth)
went in task 5.2 (#161).
"""
