"""Pure indicator functions on split-adjusted bars (the regime's and the charts').

No I/O and no configuration lookups: every window and threshold is a parameter, supplied by the
caller from ``config/risk.yaml``. Windows count sessions (bars), never calendar days.

- ``series``: rolling helpers and the aligned-series convention
- ``moving_averages``: SMA, EMA, rising and falling
- ``volume``: half-day normalization
- ``distribution``: distribution days, rally attempts and follow-through days (per index)
"""
