"""Pure guardrail checks on LLM actions (README §8; the setup system's in the archived README §8).
100% branch coverage, enforced in CI.

No I/O and no clock: the caller builds the context (equity, positions, today's setups, the
regime, the multiplier, the blocks) and logs the findings to ``guardrail_events``. Every
threshold comes from ``config/risk.yaml`` (``guardrails:``, ``regimes:``, ``entries:``).

- ``model``: outcomes (approve / clip / reject) and findings
- ``evaluate``: ``evaluate(actions, context, risk)``, every LLM action in order
- ``orders``: the 09:36 check on the real trigger, limit and stop, and the whole-share size
- ``levels``: price levels, stop width, keep losses small vs gains, the 50-day limit
- ``risk``: effective risk per regime, base count, heat, positions, exposure, no margin
- ``clusters``: industry group or 60-session correlation, at most 2 per cluster
- ``earnings``: the earnings blackout, including the estimated date
- ``positions``: tranche 2, tighten-only, exits
- ``blocks``: the kill switch, halts, crisis override, early close, data integrity
- ``book``: the momentum book's limits (#148): its halts and overlay, buys, exits, stop raises
"""
